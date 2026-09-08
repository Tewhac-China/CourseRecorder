"""视频源抽象：物理摄像头 / 本地视频文件（虚拟摄像头 + 虚拟麦克风）。

对中文或含空格的路径，优先直接交给 cv2.VideoCapture；若打开失败，
则先复制到 ASCII 临时文件（_tmp/_temp_input.mp4）再打开，退出时清理。

文件源（FileSource）在启动时用 ffmpeg 预提取整条音轨到内存，
视频循环中按帧时间戳直接取对应音频段，保证音画完全同步。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import collections
import glob
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from ..app import TEMP_DIR, TEMP_VIDEO_NAME

# ================================================================ 摄像头冲突检测与修复

# 已知会占用摄像头的进程名（小写）
_CAMERA_BLOCKERS = {
    "wechatappex.exe", "wechat.exe", "androwssvr.exe", "androwsstore.exe",
    "teams.exe", "zoom.exe", "skype.exe", "discord.exe",
    "obs64.exe", "obs32.exe", "obs.exe",
    "potplayermini.exe", "vlc.exe", "mpv.exe",
    "chrome.exe", "msedge.exe", "firefox.exe",
    "qq.exe", "bilibili.exe", "dingtalk.exe", "slack.exe", "webex.exe",
}


def _find_camera_blockers() -> List[dict]:
    """扫描占用摄像头的进程，返回 [{"pid": int, "name": str, "parent_pid": int}]。"""
    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD),
            ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wt.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if handle == -1:
        return []

    pe = PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
    blockers = []

    if kernel32.Process32First(handle, ctypes.byref(pe)):
        while True:
            name = pe.szExeFile.decode("utf-8", errors="ignore")
            if name.lower() in _CAMERA_BLOCKERS:
                blockers.append({
                    "pid": pe.th32ProcessID,
                    "name": name,
                    "parent_pid": pe.th32ParentProcessID,
                })
            if not kernel32.Process32Next(handle, ctypes.byref(pe)):
                break

    kernel32.CloseHandle(handle)
    return blockers


def _kill_process(pid: int) -> bool:
    """强制终止指定进程。"""
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/PID", str(pid), "/T"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _restart_camera_device() -> bool:
    """通过 PnP 重启摄像头设备（禁用再启用）。

    处理两种情况：
    1. 设备状态 OK 但被占用 → 重启释放
    2. 设备状态 Error（驱动崩溃）→ 重启恢复
    """
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                (
                    # 优先找 Error 状态的设备（驱动崩了）
                    "$c = Get-PnpDevice | Where-Object { $_.FriendlyName -match 'Camera' -and $_.Status -eq 'Error' }; "
                    "if (-not $c) { "
                    "  $c = Get-PnpDevice | Where-Object { $_.FriendlyName -match 'Integrated Camera' -and $_.Status -eq 'OK' } "
                    "}; "
                    "if ($c) { "
                    "  Disable-PnpDevice -InstanceId $c.InstanceId -Confirm:$false -ErrorAction SilentlyContinue; "
                    "  Start-Sleep 2; "
                    "  Enable-PnpDevice -InstanceId $c.InstanceId -Confirm:$false -ErrorAction SilentlyContinue; "
                    "  Start-Sleep 3; "
                    "  $c2 = Get-PnpDevice -InstanceId $c.InstanceId; "
                    "  Write-Host $c2.Status "
                    "} else { Write-Host 'NO_CAMERA' }"
                ),
            ],
            capture_output=True, text=True, timeout=20,
        )
        return "OK" in result.stdout
    except Exception:
        return False


def check_camera_available(timeout_sec: float = 3.0) -> Tuple[bool, str]:
    """快速检测摄像头是否可用。

    Returns:
        (True, "")  — 摄像头正常
        (False, msg) — 摄像头被占用，msg 包含诊断信息
    """
    with _camera_lock:
        # 快速尝试打开
        try:
            _co_initialize()   # 可能在后台线程调用，DSHOW 必须 COM 就绪
            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            if cap.isOpened():
                cap.release()
                return True, ""
            cap.release()
        except Exception:
            pass

        # 摄像头打不开，检查是否有已知进程占用
        blockers = _find_camera_blockers()
        if blockers:
            names = set(b["name"] for b in blockers)
            return False, f"摄像头被占用: {', '.join(names)}"
        return False, "摄像头不可用（可能被其他程序占用或驱动异常）"


def auto_fix_camera() -> Tuple[bool, str]:
    """尝试自动修复摄像头问题。

    策略：
    1. 检测占用进程并终止
    2. 检测驱动是否崩溃（Error 状态），重启设备
    3. 再次检测

    Returns:
        (True, msg) — 修复成功
        (False, msg) — 修复失败
    """
    with _camera_lock:
        _co_initialize()   # 可能在后台线程调用，DSHOW 必须 COM 就绪
        # 第一步：终止占用进程
        blockers = _find_camera_blockers()
        if blockers:
            killed = []
            for b in blockers:
                if _kill_process(b["pid"]):
                    killed.append(b["name"])
            if killed:
                time.sleep(2)

        # 第二步：检测摄像头是否恢复
        try:
            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            if cap.isOpened():
                cap.release()
                return True, "摄像头已恢复"
            cap.release()
        except Exception:
            pass

        # 第三步：重启摄像头设备（处理驱动崩溃或被占用的情况）
        if _restart_camera_device():
            time.sleep(2)
            try:
                cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
                if cap.isOpened():
                    cap.release()
                    return True, "摄像头设备已重启并恢复"
                cap.release()
            except Exception:
                pass

        return False, "自动修复失败，请在设备管理器中禁用/启用摄像头，或重启电脑"


# 摄像头操作互斥锁 — 防止扫描/预览/修复三者互相冲突
_camera_lock = threading.Lock()


def force_release_camera() -> None:
    """启动时强制释放可能残留的摄像头句柄。

    如果上次程序崩溃或被强制杀掉，摄像头设备可能没被释放。
    只尝试 DSHOW 后端的索引 0，用后台线程+超时保护。
    不阻塞调用者 — 释放失败不影响后续流程。
    """
    def _try_release():
        _co_initialize()   # 后台线程，DSHOW 必须 COM 就绪
        try:
            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            if cap.isOpened():
                cap.release()
        except Exception:
            pass

    t = threading.Thread(target=_try_release, daemon=True)
    t.start()
    t.join(timeout=1.5)  # 最多等1.5秒，不阻塞主线程


# 探测摄像头时的最大索引
_MAX_CAMERA_INDEX = 6


def _backend_fallback_order(preferred: int) -> List[int]:
    """摄像头后端尝试顺序：首选 -> 其他 -> 任意。

    实测发现某些摄像头只有 DSHOW 能出图、MSMF 会报 -1072875772，
    也有相反的情况，所以这里把三种都列上，取第一个能真正读到帧的。
    """
    order: List[int] = [preferred]
    for candidate in (cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY):
        if candidate not in order:
            order.append(candidate)
    return order


def backend_from_name(name: str | None) -> int:
    """把后端名字转成 OpenCV 常量。未知则返回 CAP_DSHOW。"""
    key = (name or "").strip().lower()
    if key in ("msmf", "cap_msmf", "media foundation"):
        return cv2.CAP_MSMF
    if key in ("any", "auto", "default"):
        return cv2.CAP_ANY
    return cv2.CAP_DSHOW


def _co_initialize() -> bool:
    """在工作线程中初始化 COM（DirectShow 的硬性要求）。

    ``CAP_DSHOW`` 是 COM 组件：任何**非主线程**里构造 ``VideoCapture``、
    调用 ``read()/set()/release()`` 之前都必须 ``CoInitialize``，否则会触发
    访问违例 (0xC0000005) 或栈缓冲溢出 (0xC0000409) 直接终止整个进程——
    表现就是"预览黑屏一下然后程序闪退"，且 Python 层捕获不到任何异常。

    不调用 ``CoUninitialize``：线程退出时 COM 会自动清理其 apartment，
    避免反初始化导致仍在主线程使用的 VideoCapture 对象失效。
    """
    if os.name != "nt":
        return False
    try:
        # COINIT_APARTMENTTHREADED
        hr = ctypes.windll.ole32.CoInitializeEx(None, 0x2)
        return hr in (0, 1)  # S_OK / S_FALSE(该线程已初始化)
    except Exception:
        return False


def _safe_set_prop(cap, prop, val, timeout: float = 3.0) -> bool:
    """带超时的属性设置：个别后端（如异常 MSMF 设备）cap.set() 也会阻塞。"""
    if cap is None:
        return False
    box: dict = {}

    def _s():
        _co_initialize()   # DSHOW 后端在子线程中必须初始化 COM
        try:
            box["r"] = cap.set(prop, val)
        except Exception:
            box["r"] = False

    th = threading.Thread(target=_s, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        return False
    return bool(box.get("r", False))


def negotiate_resolution(cap, requested: Tuple[int, int]) -> Tuple[int, int]:
    """向摄像头请求分辨率，返回驱动实际生效的分辨率。

    只做一次 set+read，不遍历全部档位（每次 set 要 ~400ms 驱动重协商）。
    如果摄像头不支持 requested，驱动会回退到它能给的最高档，
    我们直接用回读值即可。set 走带超时封装，避免个别后端阻塞。
    """
    rw, rh = int(requested[0]), int(requested[1])
    _safe_set_prop(cap, cv2.CAP_PROP_FRAME_WIDTH, rw)
    _safe_set_prop(cap, cv2.CAP_PROP_FRAME_HEIGHT, rh)
    aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if aw > 0 and ah > 0:
        return (aw, ah)
    # 兜底
    _safe_set_prop(cap, cv2.CAP_PROP_FRAME_WIDTH, 640)
    _safe_set_prop(cap, cv2.CAP_PROP_FRAME_HEIGHT, 480)
    return (640, 480)


def probe_max_resolution(cap) -> Tuple[int, int]:
    """探测摄像头支持的最高分辨率（单次请求）。

    直接请求最高档并回读驱动实际生效值 — 驱动会自动回退到
    它能支持的最高档，无需逐档尝试（每次 set 需要 ~400ms 的
    驱动重协商，逐档尝试非常耗时）。
    """
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 3840)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 2160)
    aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if aw > 0 and ah > 0:
        return (aw, ah)
    return (640, 480)  # 兜底值


class MediaSource(ABC):
    """视频源统一接口。"""

    #: 是否为文件源（决定是否需要写 raw_video.mp4）
    is_file: bool = False

    def __init__(self) -> None:
        self._opened = False

    @abstractmethod
    def start(self) -> bool:
        """打开设备/文件，返回是否成功。"""

    @abstractmethod
    def read_frame(self) -> Tuple[bool, Optional[np.ndarray], float]:
        """读取一帧，返回 (success, bgr_frame, timestamp_seconds)。"""

    @abstractmethod
    def get_fps(self) -> float:
        """标称帧率，用于计时。"""

    @abstractmethod
    def stop(self) -> None:
        """释放资源。"""

    @abstractmethod
    def list_devices(self) -> List[dict]:
        """枚举可用设备，返回 [{"index":..,"name":..,"id":..}]。"""

    # 供 UI 显示的友好名字
    def describe(self) -> str:  # pragma: no cover - 子类覆盖
        return self.__class__.__name__

    @property
    def is_opened(self) -> bool:
        return self._opened

    @property
    def frame_size(self) -> Tuple[int, int]:
        return (0, 0)


# --------------------------------------------------------------------- 摄像头
class CameraSource(MediaSource):
    """OpenCV 摄像头，独占持有，黑屏秒恢复。

    策略：
    - 每次 read 前用 grab() 刷新缓冲区，只取最新帧；
    - 黑帧 / read 失败 → 立即触发重连（不等待累积）；
    - read 超时 → 从主线程 release() 打断阻塞的 C 调用 → 重连。
    """

    is_file = False

    _READ_TIMEOUT = 5.0      # 单次 read 超时（秒，用于断流检测）。
                             # 高分辨率摄像头(如 2592x1944)采集一帧可能超过 2s,
                             # 超时过短会误判断流 → 反复重连 → 画面卡住/黑屏。
    _MAX_RECONNECT = 3       # 最大连续重连次数
    _GRAB_FLUSH = 0          # 每次 read 前 grab 的次数（0=直接 read 取最新帧）
    _OPEN_TIMEOUT = 12.0     # 打开+协商超时（秒）。实测外接摄像头打开约 5.6s,
                             # 留足余量避免偶发波动(驱动复位慢/系统负载)导致误判超时。

    def __init__(
        self,
        index: int = 0,
        width: int = 1920,
        height: int = 1080,
        fps: int = 30,
        backend: int = cv2.CAP_DSHOW,
    ) -> None:
        super().__init__()
        self.index = int(index)
        self.requested = (int(width), int(height), int(fps))
        self.backend = backend
        self.backend_used: int = backend
        self._cap: Optional[cv2.VideoCapture] = None
        self._actual_size = (0, 0)
        self._fps = float(fps)
        self._t0 = 0.0
        self._reconnect_count = 0
        self._fourcc = 0
        # 生产者线程状态：cap 在其整个生命周期只被这一个线程创建/设置/读取，
        # 杜绝跨线程调用 DSHOW 触发的原生访问越界 (0xC0000005) 闪退。
        self._cap_thread: Optional[threading.Thread] = None
        self._stop_ev = threading.Event()
        self._open_done = threading.Event()
        self._frame_ready = threading.Event()
        self._slot: "collections.deque" = collections.deque(maxlen=1)
        self._slot_lock = threading.Lock()
        self._opened = False
        self._open_error = False
        self._cap_broken = False
        # 采集子进程（进程隔离）：原生崩溃只杀子进程，不拖垮 GUI
        self._proc = None
        self._reader_thread = None
        self._proc_lock = threading.Lock()

    _MAX_START_ATTEMPTS = 2      # 启动失败重试次数（DSHOW 设备释放后复位慢）
    _START_RETRY_DELAY = 0.3     # 重试等待基数（秒），按尝试次数递增

    def _backend_try_order(self) -> list:
        """摄像头后端尝试顺序：DSHOW 优先（最稳），ANY 兜底。

        USB 摄像头在 DSHOW 下强制 MJPG 即可稳定输出最高清非黑流，
        无需 MSMF（MSMF 在个别设备上会构造死锁、拖垮启动）。
        """
        order = [cv2.CAP_DSHOW]
        if self.backend != cv2.CAP_DSHOW:
            order.append(self.backend)
        if cv2.CAP_ANY not in order:
            order.append(cv2.CAP_ANY)
        return order

    # ------------------------------------------------------------------
    # 生产者线程：独占 cap，全程在同一线程创建 / 设置 / 读取，杜绝跨线程
    # 调用 cv2.VideoCapture 触发 DSHOW 原生访问越界 (0xC0000005) 闪退。
    # ------------------------------------------------------------------
    def _launch(self, backend: int) -> bool:
        """启动采集子进程（进程隔离：DSHOW 原生崩溃只杀子进程，不拖垮 GUI）。"""
        with self._proc_lock:
            if self._stop_ev.is_set():
                return False  # 已请求停止，不再启动（避免 stop 被重试覆盖）
            self._cap_broken = False
            self._frame_ready.clear()
            with self._slot_lock:
                self._slot.clear()
            script = os.path.join(os.path.dirname(__file__), "capture_worker.py")
            args = [
                sys.executable, "-u", script,
                "--index", str(self.index),
                "--backend", str(backend),
                "--width", str(self.requested[0]),
                "--height", str(self.requested[1]),
                "--fps", str(self.requested[2]),
                "--grab-flush", "0",
            ]
            try:
                self._proc = subprocess.Popen(
                    args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                )
            except Exception as e:
                print(f"[CameraSource] 启动采集子进程失败: {e}")
                return False
            self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self._reader_thread.start()
        return True

    def _reader_loop(self) -> None:
        """读取子进程回传的 JPEG 帧流，解码后推入队列（本线程不碰 cap）。"""
        with self._proc_lock:
            proc = self._proc
            reader = self._reader_thread
        if proc is None or proc.stdout is None:
            self._cap_broken = True
            return
        # 必须用 os.read 而非 f.read：Windows 管道上 Python 的 f.read() 有内部
        # 缓冲层，会导致帧率骤降（10fps→0.3fps）。os.read 是真正的系统调用无缓冲。
        import os
        fd = proc.stdout.fileno()
        try:
            while not self._stop_ev.is_set():
                hdr = os.read(fd, 4)
                if len(hdr) < 4:
                    break  # 管道关闭 / 子进程退出（含原生崩溃）
                n = struct.unpack("<I", hdr)[0]
                if n <= 0 or n > 50_000_000:
                    break
                data = b""
                while len(data) < n:
                    chunk = os.read(fd, n - len(data))
                    if not chunk:
                        break
                    data += chunk
                if len(data) < n:
                    break
                arr = np.frombuffer(data, np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is None or frame.size == 0:
                    continue
                h, w = frame.shape[:2]
                self._actual_size = (w, h)
                with self._slot_lock:
                    self._slot.clear()
                    self._slot.append((frame, time.perf_counter() - self._t0))
                self._frame_ready.set()
        except Exception:
            pass
        # 子进程退出或管道断开 → 标记断开，由 read_frame 触发重连
        self._cap_broken = True

    def _start_with_backend(self, backend: int) -> bool:
        if not self._launch(backend):
            return False
        # 等待首帧确认采集成功；以 0.2s 轮询，可被 stop 立即打断，
        # 避免阻塞预览线程导致 stop() 无法在 5s 内终止 → QThread 崩溃。
        loops = int(self._OPEN_TIMEOUT / 0.2) + 2
        for _ in range(loops):
            if self._stop_ev.is_set():
                break
            if self._frame_ready.wait(timeout=0.2):
                self.backend_used = backend
                self._opened = True
                self._t0 = time.perf_counter()
                self._reconnect_count = 0
                return True
        # 超时：子进程未出帧（多半打开失败/崩溃），清理后换后端重试
        print(f"[CameraSource] 后端 {backend} 启动超时，放弃")
        self._stop_proc()
        return False

    def _stop_proc(self) -> None:
        self._stop_ev.set()
        with self._proc_lock:
            proc = self._proc
            self._proc = None
            reader = self._reader_thread
            self._reader_thread = None
        if proc is not None:
            # 先关闭 stdout 文件描述符，立即打断 _reader_loop 的 os.read() 阻塞，
            # 再 terminate 子进程。顺序很重要：关 fd → os.read() 立刻返回 →
            # reader 线程退出 → stop() 的 wait() 不会超时 → 不会 QThread 崩溃。
            try:
                os.close(proc.stdout.fileno())
            except Exception:
                pass
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if reader is not None and reader.is_alive():
            reader.join(timeout=2.0)

    def _safe_release(self, cap) -> None:
        """带超时的释放：个别后端（如异常 MSMF 设备）release 也会阻塞主线程。"""
        if cap is None:
            return
        def _rel():
            _co_initialize()
            try:
                cap.release()
            except Exception:
                pass

        th = threading.Thread(target=_rel, daemon=True)
        th.start()
        th.join(3.0)

    def _safe_read(self, cap, timeout=None):
        """带超时的读帧：子线程读取，主线程 join 超时则释放 cap 打断阻塞。

        返回 (ok, frame, timed_out)。用于防止个别后端（如 MSMF）在异常分辨率
        下 cap.read() 永久阻塞、拖垮整个打开/协商流程。
        """
        if timeout is None:
            timeout = self._READ_TIMEOUT
        box = {}

        def _r():
            _co_initialize()   # DSHOW 后端在子线程中必须初始化 COM，否则进程崩溃
            try:
                box["v"] = cap.read()
            except Exception:
                box["v"] = (False, None)

        th = threading.Thread(target=_r, daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            self._safe_release(cap)
            return False, None, True
        v = box.get("v", (False, None))
        if isinstance(v, tuple) and len(v) == 2:
            return v[0], v[1], False
        return False, None, False

    def _open_raw(self, backend: int) -> Optional[cv2.VideoCapture]:
        """用指定后端打开摄像头并做一次基础读取验证，失败返回 None。

        VideoCapture 构造与读帧都可能在异常后端（如某些 MSMF 设备）下永久
        阻塞，故构造也包线程超时，避免单个坏后端拖垮整个打开流程。
        """
        box: dict = {}

        def _open():
            _co_initialize()   # DSHOW 后端在子线程中必须初始化 COM，否则进程崩溃
            try:
                box["cap"] = cv2.VideoCapture(self.index, backend)
            except Exception:
                box["cap"] = None

        th = threading.Thread(target=_open, daemon=True)
        th.start()
        th.join(5.0)
        if th.is_alive():
            return None  # 构造卡死，放弃该后端
        cap = box.get("cap")
        if cap is None or not cap.isOpened():
            if cap is not None:
                self._safe_release(cap)
            return None
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        # 注意：此处绝不能读帧验证！DSHOW 首次 read 会建立 filter graph
        # 并锁定像素格式，之后再设 FOURCC=MJPG 无效（驱动回退 YUY2 → 灰度）。
        # 像素格式必须在首次 read 之前设好（见 _apply_resolution/_try），
        # "打开即卡死"的坏设备由 _try 中带超时的 _safe_read 兜底。
        return cap

    def _open_capture(self) -> Tuple[Optional[cv2.VideoCapture], int]:
        """按后端优先级同步打开摄像头。"""
        _co_initialize()   # 调用方多为预览工作线程，DSHOW 必须 COM 就绪
        for backend in _backend_fallback_order(self.backend):
            try:
                cap = cv2.VideoCapture(self.index, backend)
                if not cap.isOpened():
                    cap.release()
                    continue
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                ok, _frame = cap.read()
                if ok:
                    return cap, backend
                cap.release()
            except Exception:
                pass
        return None, self.backend

    def _apply_resolution(self, cap: cv2.VideoCapture, backend: int) -> bool:
        """在同一线程（捕获线程）内直接协商分辨率与像素格式。

        注意：本方法只在 _cap_loop 所在的捕获线程调用，cap.set()/cap.read()
        绝不跨线程，否则 DSHOW 后端会原生访问越界闪退。
        """
        w, h, fps = self.requested
        MJPG = cv2.VideoWriter_fourcc(*"MJPG")
        self._fourcc = 0

        def _is_grayscale(frame: np.ndarray) -> bool:
            """检测帧是否丢失色度（BGR 三通道无差异 → 灰度画面）。"""
            b, g, r = (frame[..., i].astype(np.int16) for i in range(3))
            return float(np.mean(np.abs(r - g)) + np.mean(np.abs(g - b))) < 0.5

        def _finalize(cw: int, ch: int) -> None:
            aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            self._actual_size = (aw, ah) if (aw > 0 and ah > 0) else (cw, ch)
            afps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
            self._fps = afps if afps > 1.0 else float(fps or 30)

        def _try(cw: int, ch: int, use_mjpg: bool) -> bool:
            if use_mjpg:
                cap.set(cv2.CAP_PROP_FOURCC, MJPG)
            if fps > 0:
                cap.set(cv2.CAP_PROP_FPS, fps)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, cw)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, ch)
            if use_mjpg:
                # 分辨率/帧率协商会把 FOURCC 重置，MJPG 必须是首次 read
                # 之前的最后一个设置（实测这是彩色与灰度的分水岭）。
                cap.set(cv2.CAP_PROP_FOURCC, MJPG)
            # 读两帧确认非黑：首帧可能是格式切换前的旧缓冲/缓存
            f = None
            for _ in range(2):
                ok, f = cap.read()
                if not (ok and f is not None and f.size > 0):
                    return False
            if not f.max() > 0:
                return False
            if use_mjpg:
                self._fourcc = MJPG
                # MJPG 后仍灰度：个别驱动需多次协商，再补救一轮
                if backend == cv2.CAP_DSHOW and _is_grayscale(f):
                    cap.set(cv2.CAP_PROP_FOURCC, MJPG)
                    for _ in range(3):
                        ok, f2 = cap.read()
                        if not (ok and f2 is not None and f2.size > 0):
                            return False
                        if not _is_grayscale(f2):
                            return True
                    print("[CameraSource] 警告: MJPG 协商后画面仍为灰度")
                return True
            # 默认格式：有画面即成功；但若丢色度（灰度）则判为失败，
            # 交由上层走 MJPG 轮补救（不给固件做多余协商）。
            if backend == cv2.CAP_DSHOW and _is_grayscale(f):
                return False
            return True

        # 第一轮：默认格式（对固件零额外折腾，绝大多数设备在此成功）
        if _try(w, h, False):
            _finalize(w, h)
            return True
        for cw, ch in ((1920, 1080), (1280, 720), (640, 480)):
            if _try(cw, ch, False):
                _finalize(cw, ch)
                return True
        # 第二轮：MJPG 补救（默认格式丢色度/黑屏的设备）
        if _try(w, h, True):
            _finalize(w, h)
            return True
        for cw, ch in ((1920, 1080), (1280, 720), (640, 480)):
            if _try(cw, ch, True):
                _finalize(cw, ch)
                return True

        # 最终兜底：所有分辨率协商都失败时，用驱动当前默认格式再读一次。
        try:
            f = None
            for _ in range(3):
                ok, f = cap.read()
                if ok and f is not None and f.size > 0:
                    self._actual_size = (int(f.shape[1]), int(f.shape[0]))
                    afps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
                    self._fps = afps if afps > 1.0 else float(fps or 30)
                    self._fourcc = int(cap.get(cv2.CAP_PROP_FOURCC) or 0)
                    print(f"[CameraSource] 兜底: 使用驱动默认格式 {self._actual_size}")
                    return True
                f = None
        except Exception as e:
            print(f"[CameraSource] 兜底读取异常: {e}")
        return False

    def start(self) -> bool:
        self.stop()
        time.sleep(0.15)
        self._stop_ev.clear()  # 开始一次全新启动，清除停止信号

        # 摄像头刚被释放（或上次异常退出）时，DSHOW 需要更长时间复位，
        # 直接再打开常常失败 → 失败后等待递增时间重试，避免"打不开/黑屏"。
        # 重试等待可被 stop 立即打断，避免预览线程卡死导致 QThread 崩溃。
        for attempt in range(1, self._MAX_START_ATTEMPTS + 1):
            if self._stop_ev.is_set():
                return False
            if self._attempt_start():
                return True
            if attempt < self._MAX_START_ATTEMPTS:
                delay = self._START_RETRY_DELAY * attempt
                print(f"[CameraSource] 启动失败，{delay:.1f}s 后重试 "
                      f"({attempt}/{self._MAX_START_ATTEMPTS})")
                for _ in range(int(delay / 0.2) + 1):
                    if self._stop_ev.is_set():
                        return False
                    time.sleep(0.2)
        self._opened = False
        return False

    def _attempt_start(self) -> bool:
        """单次启动尝试：DSHOW 优先保证能启动；MSMF 作为更高清的补充；ANY 兜底。"""
        for backend in self._backend_try_order():
            if self._stop_ev.is_set():
                return False
            if self._start_with_backend(backend):
                print(f"[CameraSource] 后端 {backend} 启动成功 size={self._actual_size}")
                return True
            print(f"[CameraSource] 后端 {backend} 打开失败（跳过）")
        return False

    def _force_release(self) -> None:
        """强制释放摄像头，忽略异常（供 check_camera_available 等外部调用）。"""
        self.stop()

    def _reconnect(self) -> bool:
        """停止采集子进程后重新打开（换后端兜底，进程隔离下原生崩溃无影响）。"""
        self._reconnect_count += 1
        if self._reconnect_count > self._MAX_RECONNECT:
            print(f"[CameraSource] 已达最大重连次数 ({self._MAX_RECONNECT})，放弃")
            self._opened = False
            return False

        print(f"[CameraSource] 重连 #{self._reconnect_count} ...")
        self._stop_proc()
        self._stop_ev.clear()  # 重连是一次全新尝试，清除停止信号
        time.sleep(0.2)  # 给摄像头短暂时间稳定
        ok = self._attempt_start()
        if not ok:
            self._opened = False
        return ok

    def read_frame(self) -> Tuple[bool, Optional[np.ndarray], float]:
        if not self._opened:
            return False, None, 0.0
        # 等待一帧；以 0.2s 轮询，可被 stop 立即打断（避免阻塞预览线程）
        if not self._frame_ready.is_set():
            loops = int(self._READ_TIMEOUT / 0.2) + 2
            got = False
            for _ in range(loops):
                if self._stop_ev.is_set():
                    break
                if self._frame_ready.wait(timeout=0.2):
                    got = True
                    break
            if not got:
                if not self._stop_ev.is_set():
                    self._reconnect()
                return False, None, 0.0
        self._frame_ready.clear()
        with self._slot_lock:
            item = self._slot[0] if self._slot else None
        if item is None:
            return False, None, 0.0
        frame, ts = item
        if self._cap_broken:
            self._reconnect()
            return False, None, ts
        if frame.max() == 0:   # 全黑帧 → 信号中断
            self._reconnect()
            return False, None, ts
        self._reconnect_count = 0
        return True, frame, ts

    def get_fps(self) -> float:
        return self._fps if self._fps > 0 else 30.0

    def stop(self) -> None:
        self._stop_proc()
        self._opened = False
        self._reconnect_count = 0

    def list_devices(self) -> List[dict]:
        return list_cameras()

    @property
    def frame_size(self) -> Tuple[int, int]:
        return self._actual_size

    def describe(self) -> str:
        w, h = self._actual_size
        return f"摄像头 #{self.index} ({w}x{h})"


# --------------------------------------------------------------------- 文件源
class FileSource(MediaSource):
    """本地视频文件作为虚拟摄像头 + 虚拟麦克风。

    - 中文/空格路径：先尝试直接打开，失败则复制到 ASCII 临时文件；
    - 时间戳按 frame_index / fps 计算（媒体时间轴），保证与音频时间轴一致；
    - ``realtime=True`` 时按媒体时间轴限速播放，模拟摄像头；
    - 启动时用 ffmpeg 预提取整条音轨（16kHz mono PCM int16）到内存，
      通过 :meth:`get_audio_chunk` 按帧时间戳取对应音频段，
      保证音画完全同步，无需独立的音频源。
    """

    is_file = True

    def __init__(
        self,
        path: str | Path,
        realtime: bool = True,
        speed: float = 1.0,
        loop: bool = False,
    ) -> None:
        super().__init__()
        self.original_path = Path(path)
        self.realtime = bool(realtime)
        self.speed = max(0.05, float(speed))
        self.loop = bool(loop)
        self._cap: Optional[cv2.VideoCapture] = None
        self._temp_copy: Optional[Path] = None
        self._fps = 30.0
        self._frame_index = 0
        self._wall_start = 0.0
        self._media_start = 0.0
        self._size = (0, 0)
        self._total_frames = 0
        # 预提取的音轨（16kHz mono int16 PCM）
        self._audio_pcm: Optional[bytes] = None
        self._audio_sr = 16000
        self._audio_sent_samples = 0

    # -------------------------------------------------- 路径处理
    def _resolve_capture(self) -> Optional[cv2.VideoCapture]:
        """返回一个已打开的 VideoCapture，必要时使用 ASCII 中转拷贝。"""
        if not self.original_path.exists():
            return None

        cap = cv2.VideoCapture(str(self.original_path))
        if cap.isOpened():
            return cap
        cap.release()

        # 直接打开失败 —— 复制到 ASCII 临时路径再试
        try:
            TEMP_DIR.mkdir(parents=True, exist_ok=True)
            dst = TEMP_DIR / TEMP_VIDEO_NAME
            if not dst.exists() or dst.stat().st_size != self.original_path.stat().st_size:
                shutil.copy2(self.original_path, dst)
            self._temp_copy = dst
            cap = cv2.VideoCapture(str(dst))
            if cap.isOpened():
                return cap
            cap.release()
        except Exception as exc:
            print(f"[FileSource] 复制/打开临时文件失败: {exc}")
        return None

    def start(self) -> bool:
        self.stop()
        cap = self._resolve_capture()
        if cap is None:
            self._opened = False
            return False

        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        self._fps = fps if fps > 1.0 else 30.0
        self._size = (
            int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
            int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
        )
        self._total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self._frame_index = 0
        self._cap = cap
        self._opened = True

        # 预提取音轨到内存（在重置时钟之前，避免提取耗时被算入帧延迟）
        self._extract_audio()
        # 时钟在首次 read_frame() 时重置，避免模型加载等启动开销被算入帧延迟
        self._clock_initialized = False

        return True

    def _extract_audio(self) -> None:
        """用 ffmpeg 一次性提取整条音轨（16kHz mono int16 PCM）到内存。"""
        src = str(self.original_path)
        try:
            proc = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", src,
                    "-vn", "-ar", str(self._audio_sr), "-ac", "1",
                    "-f", "s16le", "-",
                ],
                capture_output=True, timeout=120,
            )
            if proc.returncode == 0 and len(proc.stdout) > 0:
                self._audio_pcm = proc.stdout
                dur = len(proc.stdout) / (self._audio_sr * 2)
                print(f"[FileSource] 音轨已提取: {dur:.1f}s ({len(proc.stdout)} bytes)")
            else:
                self._audio_pcm = None
                print(f"[FileSource] 音轨提取失败（无音频流或格式不支持）")
        except Exception as exc:
            self._audio_pcm = None
            print(f"[FileSource] 音轨提取异常: {exc}")

    def get_audio_chunk(self, timestamp: float, duration: float = 0.1) -> Optional[bytes]:
        """按时间戳取对应音频段（int16 PCM bytes）。

        与视频帧时间戳精确对应，保证音画同步。
        每次调用自动推进已发送位置，避免重复发送。
        """
        if self._audio_pcm is None:
            return None
        bytes_per_sample = 2  # int16
        start_sample = int(timestamp * self._audio_sr)
        end_sample = int((timestamp + duration) * self._audio_sr)
        start_byte = start_sample * bytes_per_sample
        end_byte = end_sample * bytes_per_sample
        if start_byte >= len(self._audio_pcm):
            return None
        end_byte = min(end_byte, len(self._audio_pcm))
        if start_byte >= end_byte:
            return None
        return self._audio_pcm[start_byte:end_byte]

    def has_audio(self) -> bool:
        return self._audio_pcm is not None and len(self._audio_pcm) > 0

    def _reset_clock(self, media_ts: float) -> None:
        self._media_start = media_ts
        self._wall_start = time.perf_counter()

    def read_frame(self) -> Tuple[bool, Optional[np.ndarray], float]:
        if not self._cap:
            return False, None, 0.0

        # 首次读取时初始化时钟（避免启动开销被算入帧延迟）
        if not self._clock_initialized:
            self._reset_clock(0.0)
            self._clock_initialized = True

        # ---- 跳帧：处理太慢导致落后时，快进到当前实时位置 ----
        if self.realtime and self._fps > 0:
            wall_elapsed = time.perf_counter() - self._wall_start
            media_elapsed = wall_elapsed * self.speed
            expected_idx = int((self._media_start + media_elapsed) * self._fps)
            frames_behind = expected_idx - self._frame_index
            if frames_behind > 2:
                target_idx = max(0, expected_idx - 1)
                self._cap.set(cv2.CAP_PROP_POS_FRAMES, target_idx)
                self._frame_index = target_idx

        ok, frame = self._cap.read()
        if not ok or frame is None:
            if self.loop and self._total_frames > 0:
                self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                self._frame_index = 0
                self._reset_clock(0.0)
                ok, frame = self._cap.read()
                if not ok or frame is None:
                    return False, None, 0.0
            else:
                ts = self._frame_index / self._fps if self._fps else 0.0
                return False, None, ts

        ts = self._frame_index / self._fps if self._fps else 0.0
        self._frame_index += 1

        if self.realtime:
            target = self._wall_start + (ts - self._media_start) / self.speed
            delay = target - time.perf_counter()
            if delay > 0:
                deadline = time.perf_counter() + delay
                while time.perf_counter() < deadline:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        break
                    time.sleep(min(0.02, remaining))
        return True, frame, ts

    def seek_seconds(self, seconds: float) -> None:
        """跳转到指定秒（仅在未启动或已启动时都可用）。"""
        if not self._cap or not self._fps:
            return
        idx = max(0, int(seconds * self._fps))
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        self._frame_index = idx
        self._reset_clock(seconds)

    def get_fps(self) -> float:
        return self._fps

    @property
    def total_frames(self) -> int:
        return self._total_frames

    @property
    def duration(self) -> float:
        return (self._total_frames / self._fps) if self._fps else 0.0

    def stop(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
        self._cap = None
        self._opened = False
        self._audio_pcm = None
        self._audio_sent_samples = 0
        self.cleanup_temp()

    def cleanup_temp(self) -> None:
        """删除中转拷贝（若存在）。"""
        if self._temp_copy and self._temp_copy.exists():
            try:
                self._temp_copy.unlink()
            except Exception:
                pass
        self._temp_copy = None

    def list_devices(self) -> List[dict]:
        return []

    @property
    def frame_size(self) -> Tuple[int, int]:
        return self._size

    def describe(self) -> str:
        return f"视频文件 {self.original_path.name}"


# --------------------------------------------------------------------- 空源（不启用画面记录）
class NullSource(MediaSource):
    """空视频源：不录制画面，仅保持音频转写运行。

    用于"不启用画面记录"模式 — read_frame() 阻塞直到 stop() 被调用，
    使视频线程保持存活但不产出帧，音频通过独立的 AudioSource 回调处理。
    """

    is_file = False

    def __init__(self) -> None:
        super().__init__()
        self._stop_event = threading.Event()
        self._t0 = 0.0

    def start(self) -> bool:
        self._stop_event.clear()
        self._opened = True
        self._t0 = time.perf_counter()
        return True

    def read_frame(self) -> Tuple[bool, Optional[np.ndarray], float]:
        # 阻塞直到被停止，返回 (False, None, ts) 使视频循环 continue
        self._stop_event.wait(timeout=0.1)
        ts = time.perf_counter() - self._t0
        return False, None, ts

    def get_fps(self) -> float:
        return 30.0

    def stop(self) -> None:
        self._stop_event.set()
        self._opened = False

    def list_devices(self) -> List[dict]:
        return []

    def describe(self) -> str:
        return "不启用画面记录"


# --------------------------------------------------------------------- 工具
def _scan_cameras_with_backend(
    max_index: int, backend: int, backend_name: str
) -> List[dict]:
    """用指定后端扫描摄像头，返回能真正读到帧的设备。

    每个索引有超时保护：用后台线程读帧，主线程等待最多 2 秒，
    避免某个卡死的摄像头拖垮整个扫描。
    """
    if not _camera_lock.acquire(timeout=5):
        print(f"[Camera] 扫描 {backend_name}: 获取锁超时，跳过")
        return []
    try:
        return _do_scan_cameras(max_index, backend, backend_name)
    finally:
        _camera_lock.release()


def _do_scan_cameras(
    max_index: int, backend: int, backend_name: str
) -> List[dict]:
    """实际扫描逻辑（调用者需持有 _camera_lock）。

    每个索引在【同一个线程】内完成 open / 读帧 / 探测最高分辨率，
    避免 cap 被跨线程使用触发 DSHOW 原生崩溃 (0xC0000005)。扫描本身已在
    独立子进程中，但单线程仍是最稳的做法，能保证稳定返回设备列表。
    """
    devices: List[dict] = []

    def _probe(idx: int):
        """在单一线程内打开并验证摄像头，返回设备字典或 None。"""
        _co_initialize()   # DSHOW 必须 COM 就绪
        cap = None
        try:
            cap = cv2.VideoCapture(idx, backend)
            if cap is None or not cap.isOpened():
                return None
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            # 读一帧验证能出图（open 成功不代表能读到帧）
            f = None
            for _ in range(3):
                ok, f = cap.read()
                if ok and f is not None and f.size > 0:
                    break
                f = None
            if f is None:
                return None
            h, w = f.shape[:2]
            # 单次协商探测摄像头支持的最高分辨率（在同一线程内）
            max_w, max_h = probe_max_resolution(cap)
            return {
                "index": idx,
                "id": f"camera:{idx}",
                "name": f"摄像头 #{idx} ({max_w}x{max_h}, {backend_name})",
                "max_width": int(max_w),
                "max_height": int(max_h),
                "width": int(w),
                "height": int(h),
                "default_width": int(w),
                "default_height": int(h),
                "backend": backend_name,
            }
        except Exception:
            return None
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
            # 给驱动时间回收资源，降低连锁卡死概率
            time.sleep(0.15)

    for idx in range(max_index):
        box = [None]

        def _run(i, b):
            b[0] = _probe(i)

        th = threading.Thread(target=_run, args=(idx, box), daemon=True)
        th.start()
        th.join(timeout=4.0)  # 每个索引最多 4 秒（含 open / 读 / 探测）
        if th.is_alive():
            # 探测卡死：等待僵尸线程释放，再继续扫描后续索引
            print(f"[Camera] idx={idx} 探测卡死（{backend_name}），跳过")
            th.join(timeout=1.0)
            continue
        if box[0] is not None:
            devices.append(box[0])
    return devices


def _pnp_camera_names() -> List[str]:
    """返回 PnP 'Camera' 类设备的友好名列表。

    用途：
    - 限定摄像头扫描范围（避免盲扫 8 个索引的漫长过程）；
    - 为扫描到的设备提供 PnP 友好名（比"摄像头 #N"更友好）。
    """
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-PnpDevice -Class Camera -Status OK -ErrorAction SilentlyContinue "
             "| Select-Object -ExpandProperty FriendlyName)"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            names = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
            return names
    except Exception:
        pass
    return []


def _get_physical_camera_count() -> int:
    """通过 PnP 设备列表获取物理摄像头数量。"""
    return len(_pnp_camera_names())


def list_cameras(max_index: int = _MAX_CAMERA_INDEX) -> List[dict]:
    """枚举可用摄像头（快速版）。

    优化点（原来扫 8 索引 × 2 后端 × 每索引 open+read+5 档协商，非常慢）：
    1. 先用 PowerShell 枚举 PnP 摄像头（~0.5s），据此限定扫描范围，
       不再盲扫 8 个索引；
    2. 只做 DSHOW 主扫；MSMF 仅补扫 DSHOW 没找到的索引；
    3. 分辨率探测改为单次请求回读（不再逐 5 档协商）；
    4. 名称优先使用 PnP 友好名，而不是干巴巴的"摄像头 #N"。
    """
    pnp_names = _pnp_camera_names()
    # IR 摄像头（如 Integrated IR Camera）通常不暴露为标准捕获设备，
    # 不能按索引对应 PnP 名单，参与数量判断与命名时都要剔除
    capture_names = [n for n in pnp_names if "ir camera" not in n.lower()]
    limit = min(max_index, 4)  # PnP 枚举失败时的保守上限，避免盲扫过多索引
    if pnp_names:
        limit = min(max_index, max(1, len(pnp_names) + 2))

    dshow_devices = _scan_cameras_with_backend(limit, cv2.CAP_DSHOW, "dshow")

    # MSMF 补扫：只有当 DSHOW 找到的数量少于 PnP 报告的可用摄像头数量
    # （说明有摄像头 DSHOW 打不开）时才补扫；本机 DSHOW 已找齐则跳过，
    # 省去一轮逐索引探测。PnP 枚举失败时保守起见按老逻辑补扫。
    msmf_devices: List[dict] = []
    if pnp_names:
        need_msmf = len(dshow_devices) < max(1, len(capture_names))
    else:
        need_msmf = len(dshow_devices) < limit
    if need_msmf:
        msmf_devices = _scan_cameras_with_backend(limit, cv2.CAP_MSMF, "msmf")

    # 合并：DSHOW 优先，MSMF 补充 DSHOW 未找到的索引
    seen = {d["index"] for d in dshow_devices}
    merged = list(dshow_devices)
    for d in msmf_devices:
        if d["index"] not in seen:
            merged.append(d)

    # 用 PnP 友好名命名：DSHOW 捕获设备是从真实摄像头开始编号的，
    # 而 capture_names 已剔除 IR 等不暴露为捕获设备的项，因此两者顺序大致
    # 一致，按序对应命名（比"摄像头 #N"友好得多）
    if capture_names:
        for d in merged:
            idx = d["index"]
            if idx < len(capture_names):
                d["name"] = f"{capture_names[idx]} ({d['max_width']}x{d['max_height']}, {d['backend']})"

    # 合并结果即最终结果：DSHOW 优先，MSMF 只补 DSHOW 缺失的索引
    return merged


def probe_first_camera() -> Optional[int]:
    """返回第一个可用摄像头索引，没有则 None。"""
    cams = list_cameras()
    return cams[0]["index"] if cams else None


def create_video_source(cfg: dict, resolution_mode: str = "max") -> Optional[MediaSource]:
    """根据配置字典创建视频源。

    Args:
        cfg: 视频源配置字典
        resolution_mode: 分辨率模式 - "max" 使用最高分辨率, "1080p" 降采样到1080P
    """
    stype = (cfg or {}).get("type", "camera")
    if stype == "none":
        return NullSource()
    if stype == "nativecam":
        # 手机摄像头（USB / adb forward，原生最高分辨率）
        from .nativecam_source import NativeCamSource
        return NativeCamSource(port=int(cfg.get("port", 8888) or 8888))
    if stype == "file":
        path = (cfg or {}).get("path") or ""
        if not path:
            return None
        return FileSource(
            path,
            realtime=bool(cfg.get("realtime", True)),
            speed=float(cfg.get("speed", 1.0) or 1.0),
            loop=bool(cfg.get("loop", False)),
        )
    # 根据分辨率模式选择宽高
    if resolution_mode == "max":
        # 使用摄像头最高分辨率
        width = int(cfg.get("max_width", 0) or 0)
        height = int(cfg.get("max_height", 0) or 0)
        if width <= 0 or height <= 0:
            # 没有记录最高分辨率，使用默认1080P
            width, height = 1920, 1080
    else:
        # 降采样到1080P
        width, height = 1920, 1080
    return CameraSource(
        index=int(cfg.get("index", 0) or 0),
        width=width,
        height=height,
        fps=int(cfg.get("fps", 30) or 30),
        backend=backend_from_name(cfg.get("backend")),
    )
