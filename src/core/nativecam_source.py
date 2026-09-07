"""NativeCam 手机摄像头视频源（USB / adb forward）。

集成项目自带的 ``ADB/NativeCam``：
- 手机端 App（com.nativecam.bridge）在 127.0.0.1:8888 提供 HTTP 服务
  （/info /frame /sizes /setres /restart /stop）；
- PC 端通过 ``adb forward tcp:8888 tcp:8888`` 走 USB 取帧，不依赖 WiFi；
- 帧为摄像头**原生最高分辨率** JPEG，不做降采样。

连接流程（:func:`connect_nativecam`）全自动：
  找 adb → 检测 USB 手机 → adb forward → 安装 APK（若未装，用项目内
  ADB/NativeCam/build/nativecam.apk）→ 授予相机权限 → 启动前台服务 →
  等待 /info 就绪（相机被策略禁用时自动 restart 一次）。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from .media_source import MediaSource

DEFAULT_PORT = 8888

_PACKAGE = "com.nativecam.bridge"
_SERVICE = _PACKAGE + "/.CameraService"
_CAMERA_PERMISSION = "android.permission.CAMERA"

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APK_PATH = _PROJECT_ROOT / "ADB" / "NativeCam" / "build" / "nativecam.apk"

# adb 查找顺序：环境变量 → 项目内置 scrcpy → PATH
_ADB_CANDIDATES = [
    os.environ.get("NATIVECAM_ADB", ""),
    str(_PROJECT_ROOT / "ADB" / "scrcpy" / "scrcpy-win64-v4.1" / "adb.exe"),
    "adb",
]


# ================================================================ adb 工具
def find_adb() -> Optional[str]:
    """按候选顺序查找可用的 adb.exe。"""
    for p in _ADB_CANDIDATES:
        if not p:
            continue
        try:
            if os.path.isabs(p) and not os.path.exists(p):
                continue
            r = subprocess.run(
                [p, "version"], capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                return p
        except Exception:
            continue
    return None


def _run_adb(adb: str, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [adb, *args], capture_output=True, text=True, timeout=timeout,
    )


def adb_start_server(adb: str) -> bool:
    """确保 adb server 已启动（首次调用会拉起 server，可能需要几秒）。"""
    try:
        _run_adb(adb, "start-server", timeout=30)
        return True
    except Exception:
        return False


def adb_device_id(adb: str) -> Tuple[bool, str]:
    """检测 USB 连接的手机。

    Returns:
        (True, device_id) — 已连接并授权的设备
        (False, error_msg) — 无设备 / 未授权 / 离线
    """

    def _parse(out: str):
        lines = out.strip().splitlines()[1:]  # 跳过 "List of devices attached"
        for line in lines:
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1] == "device":
                return True, parts[0]
        for line in lines:
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1] == "unauthorized":
                return False, "手机已连接但未授权，请在手机上允许 USB 调试"
            if len(parts) >= 2 and parts[1] == "offline":
                return False, "手机已连接但离线，请重新插拔 USB 线"
        return None  # 完全未检测到任何设备

    try:
        # 首次调用需拉起 adb server，预留足够超时
        r = _run_adb(adb, "devices", timeout=20)
    except Exception as e:
        return False, f"adb 检测异常: {e}"
    if r.returncode != 0:
        return False, f"adb 执行失败: {r.stderr.strip()}"

    res = _parse(r.stdout)
    if res is not None:
        return res

    # 完全没检测到设备：adb server 偶发卡死会导致识别失败，重启后再试一次
    try:
        _run_adb(adb, "kill-server", timeout=10)
        adb_start_server(adb)
        r2 = _run_adb(adb, "devices", timeout=20)
        if r2.returncode == 0:
            res = _parse(r2.stdout)
            if res is not None:
                return res
    except Exception:
        pass

    return False, "未检测到 USB 手机，请确认：\n1. USB 线已连接\n2. 手机已开启 USB 调试\n3. 手机上已授权此电脑"


def adb_forward(adb: str, port: int = DEFAULT_PORT) -> bool:
    """建立 adb forward（USB 数据通道）。"""
    try:
        _run_adb(adb, "forward", "--remove", f"tcp:{port}", timeout=5)
        r = _run_adb(adb, "forward", f"tcp:{port}", f"tcp:{port}", timeout=5)
        return r.returncode == 0
    except Exception:
        return False


# ================================================================ HTTP 工具
def _http_get(url: str, timeout: float = 10.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


def _http_json(url: str, timeout: float = 5.0) -> Optional[dict]:
    try:
        return json.loads(_http_get(url, timeout=timeout))
    except Exception:
        return None


def nativecam_info(port: int = DEFAULT_PORT) -> Optional[dict]:
    """查询手机端服务状态（None 表示不可达）。"""
    return _http_json(f"http://127.0.0.1:{port}/info")


# ================================================================ 全自动连接
def connect_nativecam(port: int = DEFAULT_PORT) -> Tuple[bool, Optional[dict], str]:
    """全自动手机摄像头 USB 连接流程。

    Returns:
        (True, cfg, msg) — 连接成功，cfg 可直接传给 create_video_source
        (False, None, msg) — 连接失败，msg 包含诊断信息
    """
    adb = find_adb()
    if not adb:
        return False, None, (
            "未找到 adb.exe\n"
            f"请确认项目 ADB 目录完整（{ _PROJECT_ROOT / 'ADB' }），\n"
            "或设置环境变量 NATIVECAM_ADB 指向 adb.exe"
        )

    ok, msg = adb_device_id(adb)
    if not ok:
        return False, None, msg

    adb_start_server(adb)  # 确保 adb server 存活（forward 依赖它）
    if not adb_forward(adb, port):
        return False, None, "adb forward 失败，请重新插拔 USB 线后重试"

    base = f"http://127.0.0.1:{port}"
    info = nativecam_info(port)
    if info is None or info.get("status") != "streaming":
        # ---- 服务未运行：安装（若需要）→ 授权 → 启动 ----
        try:
            r = _run_adb(adb, "shell", "pm", "list", "packages", _PACKAGE)
            installed = _PACKAGE in (r.stdout or "")
        except Exception:
            installed = False

        if not installed and not _APK_PATH.exists():
            return False, None, (
                "手机上未安装 NativeCam App，且未找到 APK：\n"
                f"{_APK_PATH}\n请先运行 ADB/NativeCam/build.bat 构建"
            )
        # 总是升级安装 (-r 保留数据、幂等且只需几秒):
        # 确保 APK 版本与 PC 端功能匹配 —— 旧版本不含 region/3A 扩展,
        # 不升级会导致"点选对焦/自动曝光"等新功能看起来完全没用。
        if _APK_PATH.exists():
            try:
                r = _run_adb(adb, "install", "-r", str(_APK_PATH), timeout=120)
            except Exception as e:
                return False, None, f"APK 安装异常: {e}"
            if r.returncode != 0:
                return False, None, f"APK 安装失败: {(r.stderr or r.stdout).strip()}"

        _run_adb(adb, "shell", "pm", "grant", _PACKAGE, _CAMERA_PERMISSION)
        try:
            _run_adb(adb, "shell", "am", "start-foreground-service", "-n", _SERVICE)
        except Exception as e:
            return False, None, f"启动手机端服务异常: {e}"

        # 等待服务就绪（最多 15 秒）
        for _ in range(15):
            time.sleep(1.0)
            info = nativecam_info(port)
            if info and info.get("status") == "streaming":
                break
        if info is None or info.get("status") != "streaming":
            # 重装后首次启动可能出现“相机被策略禁用”，restart 一次即可恢复
            try:
                _http_get(base + "/restart", timeout=8)
            except Exception:
                pass
            for _ in range(5):
                time.sleep(1.0)
                info = nativecam_info(port)
                if info and info.get("status") == "streaming":
                    break
            if info is None or info.get("status") != "streaming":
                err = (info or {}).get("lastError", "未知错误")
                return False, None, f"手机端 NativeCam 服务未就绪: {err}"

    w = int(info.get("width") or 0)
    h = int(info.get("height") or 0)
    fps = float(info.get("fps") or 0)
    cfg = {
        "type": "nativecam",
        "name": f"📱 手机摄像头 ({w}x{h}, USB)",
        "width": w,
        "height": h,
        "max_width": w,
        "max_height": h,
        "port": port,
    }
    print(f"[NativeCam] 已连接: {w}x{h} @ {fps:.1f}fps (USB)")
    return True, cfg, "手机摄像头已连接（USB）"


# ================================================================ 视频源
class NativeCamSource(MediaSource):
    """手机摄像头视频源：HTTP /frame 拉取原生分辨率 JPEG。

    每帧一次 HTTP 请求（走 adb forward USB 通道），帧率由请求耗时
    自然限速；无 OpenCV 设备索引，不与物理摄像头抢占。
    """

    is_file = False

    _READ_TIMEOUT = 15.0      # 单帧拉取超时（秒）
    _MAX_CONSECUTIVE_FAILS = 10  # 连续失败次数上限（超过则标记断开）
    _FAILS_BEFORE_RESTART = 3    # 连续失败达到该次数 → 远程重启手机端相机

    def __init__(self, port: int = DEFAULT_PORT, fps: float = 15.0, quality: int = 95) -> None:
        super().__init__()
        self.port = int(port)
        self.base = f"http://127.0.0.1:{self.port}"
        self._fps = float(fps)
        self._quality = int(quality)
        self._t0 = 0.0
        self._fails = 0
        self._size = (0, 0)

    def start(self) -> bool:
        adb = find_adb()
        info = nativecam_info(self.port)
        if (info is None or info.get("status") != "streaming") and adb:
            # 尝试自动拉起：forward + start service
            adb_forward(adb, self.port)
            try:
                _run_adb(adb, "shell", "am", "start-foreground-service", "-n", _SERVICE)
            except Exception:
                pass
            for _ in range(10):
                time.sleep(1.0)
                info = nativecam_info(self.port)
                if info and info.get("status") == "streaming":
                    break

        if info is None or info.get("status") != "streaming":
            self._opened = False
            return False

        # 设置 JPEG 质量（提高画质，减少白色噪点）
        if self._quality > 0:
            self._set_quality(self._quality)

        w = int(info.get("width") or 0)
        h = int(info.get("height") or 0)
        if w > 0 and h > 0:
            self._size = (w, h)
        reported_fps = float(info.get("fps") or 0)
        if 0 < reported_fps < 60:
            self._fps = reported_fps
        self._opened = True
        self._fails = 0
        self._t0 = time.perf_counter()
        return True

    def _set_quality(self, quality: int) -> None:
        """设置手机端 JPEG 质量（1-100）。"""
        try:
            _http_get(f"{self.base}/setquality?q={quality}", timeout=5.0)
        except Exception as exc:
            print(f"[NativeCam] 设置质量失败: {exc}")

    def set_resolution(self, size: str) -> bool:
        """设置手机端分辨率（如 '1920x1080' 或 'auto'）。"""
        try:
            _http_get(f"{self.base}/setres?size={size}", timeout=5.0)
            time.sleep(1.0)
            info = nativecam_info(self.port)
            if info and info.get("status") == "streaming":
                w = int(info.get("width") or 0)
                h = int(info.get("height") or 0)
                if w > 0 and h > 0:
                    self._size = (w, h)
                return True
        except Exception as exc:
            print(f"[NativeCam] 设置分辨率失败: {exc}")
        return False

    def _try_restart(self) -> bool:
        """远程重启手机端相机并等待恢复（取代手动在手机 App 上点"重启相机"）。

        手机端服务运行一段时间后相机偶发挂起（画面卡住），重启即可恢复。
        阻塞等待恢复（最多约 8 秒），期间调用方只是拿帧变慢，不算失败。
        """
        print("[NativeCam] 画面无响应，自动重启手机端相机...")
        try:
            _http_get(self.base + "/restart", timeout=8)
        except Exception as exc:
            print(f"[NativeCam] 重启请求失败: {exc}")
        for _ in range(16):
            time.sleep(0.5)
            info = nativecam_info(self.port)
            if info and info.get("status") == "streaming":
                print("[NativeCam] 手机端相机已恢复")
                return True
        print("[NativeCam] 重启后仍未恢复")
        return False

    def read_frame(self) -> Tuple[bool, Optional[np.ndarray], float]:
        if not self._opened:
            return False, None, 0.0
        ts = time.perf_counter() - self._t0
        try:
            jpg = _http_get(self.base + "/frame", self._READ_TIMEOUT)
            img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                raise RuntimeError("JPEG 解码失败")
            self._fails = 0
            h, w = img.shape[:2]
            self._size = (w, h)
            return True, img, ts
        except Exception as exc:
            self._fails += 1
            print(f"[NativeCam] 取帧失败({self._fails}/{self._MAX_CONSECUTIVE_FAILS}): {exc}")
            # 连续失败（画面卡住的典型表现）→ 自动重启手机端相机
            if self._fails == self._FAILS_BEFORE_RESTART:
                self._try_restart()
            if self._fails >= self._MAX_CONSECUTIVE_FAILS:
                self._opened = False
            return False, None, ts

    def get_fps(self) -> float:
        return self._fps if self._fps > 0 else 15.0

    def stop(self) -> None:
        # 只停止取帧，不停止手机端服务（便于快速重开）
        self._opened = False
        self._fails = 0

    def list_devices(self) -> List[dict]:
        # 手机摄像头不参与系统摄像头枚举，在 UI 中作为独立选项出现
        return []

    @property
    def frame_size(self) -> Tuple[int, int]:
        return self._size

    def describe(self) -> str:
        w, h = self._size
        return f"手机摄像头 ({w}x{h}, USB)"
