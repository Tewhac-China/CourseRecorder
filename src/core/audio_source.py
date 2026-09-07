"""音频源抽象：麦克风 / 系统回环 / 视频文件音轨。

Windows 下：
- 麦克风：sounddevice InputStream
- 系统回环：sounddevice RawInputStream + WasapiSettings(loopback=True)
- 视频文件：ffmpeg 子进程解码 PCM
"""

from __future__ import annotations

import subprocess
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from queue import Queue
from typing import Callable, List, Optional, Tuple

import numpy as np

# sounddevice 在本环境已手动修复；若未来缺失可回退到 soundcard
try:
    import sounddevice as sd
except Exception:  # pragma: no cover
    sd = None  # type: ignore

_SAMPLE_RATE = 16000
_CHANNELS = 1
_BLOCK_SIZE = 1024

AudioCallback = Callable[[bytes, float], None]


def list_audio_devices(kind: str = "input") -> List[dict]:
    """枚举音频设备。

    kind: "input" | "output" | "all"
    返回 [{index, name, hostapi, is_loopback_capable}]，index 为 sounddevice 使用的设备号。
    """
    if sd is None:
        return []
    devices: List[dict] = []
    try:
        devs = sd.query_devices()
        apis = sd.query_hostapis()
        for i, d in enumerate(devs):
            max_in = int(d.get("max_input_channels", 0))
            max_out = int(d.get("max_output_channels", 0))
            is_input = max_in > 0
            is_output = max_out > 0
            if kind == "input" and not is_input:
                continue
            if kind == "output" and not is_output:
                continue
            hostapi = apis[d["hostapi"]]["name"] if 0 <= d["hostapi"] < len(apis) else ""
            devices.append(
                {
                    "index": i,
                    "name": str(d.get("name", "")),
                    "hostapi": hostapi,
                    "max_input_channels": max_in,
                    "max_output_channels": max_out,
                    "is_loopback_capable": is_output and hostapi.lower() == "windows wasapi",
                }
            )
    except Exception as exc:
        print(f"[Audio] 枚举设备失败: {exc}")
    return devices


class AudioSource(ABC):
    """音频源统一接口。"""

    def __init__(self, sample_rate: int = _SAMPLE_RATE, channels: int = _CHANNELS) -> None:
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self._callback: Optional[AudioCallback] = None
        self._running = False

    @abstractmethod
    def start(self, callback: AudioCallback) -> bool:
        """开始采集，成功返回 True。"""

    @abstractmethod
    def stop(self) -> None:
        """停止采集。"""

    def is_running(self) -> bool:
        return self._running


class MicrophoneAudioSource(AudioSource):
    """麦克风输入。"""

    def __init__(
        self,
        device_index: Optional[int] = None,
        sample_rate: int = _SAMPLE_RATE,
        channels: int = _CHANNELS,
        block_size: int = _BLOCK_SIZE,
    ) -> None:
        super().__init__(sample_rate, channels)
        self.device_index = device_index
        self.block_size = int(block_size)
        self._stream: Optional[sd.InputStream] = None  # type: ignore
        self._t0 = 0.0

    def start(self, callback: AudioCallback) -> bool:
        if sd is None:
            return False
        self.stop()
        self._callback = callback
        self._t0 = time.perf_counter()
        try:
            self._stream = sd.InputStream(
                device=self.device_index,
                channels=self.channels,
                samplerate=self.sample_rate,
                blocksize=self.block_size,
                dtype="int16",
                callback=self._sd_callback,
            )
            self._stream.start()
            self._running = True
            print(f"[MicSource] 麦克风启动成功: device={self.device_index}, sr={self.sample_rate}Hz")
            return True
        except Exception as exc:
            print(f"[MicSource] 启动失败: {exc}")
            self._running = False
            return False

    def _sd_callback(self, indata: np.ndarray, frames: int, _time_info, status) -> None:
        if status:
            print(f"[MicSource] status: {status}")
        if self._callback is None:
            return
        # indata shape: (frames, channels)
        mono = indata[:, 0] if indata.ndim > 1 and indata.shape[1] > 1 else indata.ravel()
        ts = time.perf_counter() - self._t0
        self._callback(mono.astype(np.int16).tobytes(), ts)

    def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None


class LoopbackAudioSource(AudioSource):
    """Windows WASAPI 系统回环录制。"""

    def __init__(
        self,
        device_index: Optional[int] = None,
        sample_rate: int = _SAMPLE_RATE,
        channels: int = _CHANNELS,
        block_size: int = _BLOCK_SIZE,
    ) -> None:
        super().__init__(sample_rate, channels)
        self.device_index = device_index
        self.block_size = int(block_size)
        self._stream: Optional[sd.RawInputStream] = None  # type: ignore
        self._t0 = 0.0

    def start(self, callback: AudioCallback) -> bool:
        if sd is None:
            return False
        self.stop()
        self._callback = callback
        self._t0 = time.perf_counter()
        try:
            # 选择 WASAPI hostapi；device 指定输出设备（扬声器）
            extra = sd.WasapiSettings(loopback=True)
            self._stream = sd.RawInputStream(
                device=self.device_index,
                channels=self.channels,
                samplerate=self.sample_rate,
                blocksize=self.block_size,
                dtype="int16",
                callback=self._sd_callback,
                extra_settings=extra,
            )
            self._stream.start()
            self._running = True
            print(f"[LoopbackSource] 系统回环启动成功: device={self.device_index}, sr={self.sample_rate}Hz")
            return True
        except Exception as exc:
            print(f"[LoopbackSource] 启动失败: {exc}")
            self._running = False
            return False

    def _sd_callback(self, indata: np.ndarray, frames: int, _time_info, status) -> None:
        if status:
            print(f"[LoopbackSource] status: {status}")
        if self._callback is None:
            return
        ts = time.perf_counter() - self._t0
        self._callback(indata.tobytes(), ts)

    def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None


class FileAudioSource(AudioSource):
    """通过 ffmpeg 从视频文件解码音频。

    输出格式：16-bit signed little-endian PCM，单声道，16 kHz。
    以接近实时速度推送（也可一次性快速推完，取决于 realtime 参数）。

    Round-2：ffmpeg 的解码速度会略微快于/慢于视频画面，用字节偏移推算的
    timestamp 会与画面时间轴产生累积偏差。因此允许外部注入
    ``timestamp_provider``（通常返回视频线程当前的画面时间戳）作为基准。
    """

    def __init__(
        self,
        path: str | Path,
        sample_rate: int = _SAMPLE_RATE,
        channels: int = _CHANNELS,
        realtime: bool = True,
        speed: float = 1.0,
        timestamp_provider: Optional[Callable[[], float]] = None,
    ) -> None:
        super().__init__(sample_rate, channels)
        self.path = Path(path)
        self.realtime = bool(realtime)
        self.speed = max(0.05, float(speed))
        self.timestamp_provider = timestamp_provider
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self, callback: AudioCallback) -> bool:
        if not self.path.exists():
            print(f"[FileAudioSource] 文件不存在: {self.path}")
            return False
        self.stop()
        self._callback = callback
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._decode_loop, daemon=True)
        self._thread.start()
        self._running = True
        return True

    def _decode_loop(self) -> None:
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel", "error",
            "-i", str(self.path),
            "-vn",
            "-ar", str(self.sample_rate),
            "-ac", str(self.channels),
            "-f", "s16le",
            "-",
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as exc:
            print(f"[FileAudioSource] ffmpeg 启动失败: {exc}")
            self._running = False
            return

        block_bytes = self.blocksize_bytes()
        start_time = time.perf_counter()
        samples_read = 0
        while not self._stop_event.is_set():
            try:
                chunk = proc.stdout.read(block_bytes)  # type: ignore
            except Exception:
                break
            if not chunk:
                break
            if self._callback is not None:
                self._callback(chunk, self._next_timestamp(samples_read))
            samples_read += len(chunk) // 2  # int16 = 2 bytes
            if self.realtime:
                target = start_time + (samples_read / self.sample_rate) / self.speed
                delay = target - time.perf_counter()
                if delay > 0:
                    time.sleep(min(0.05, delay))
        try:
            proc.stdout.close()  # type: ignore
            proc.wait(timeout=2)
        except Exception:
            proc.kill()
        self._running = False

    def _next_timestamp(self, samples_read: int) -> float:
        """本块音频使用的时间戳。

        优先使用外部提供的视频画面时间戳（保证与幻灯片时间轴对齐），
        否则退回按已解码采样数推算。
        """
        if self.timestamp_provider is not None:
            try:
                return float(self.timestamp_provider())
            except Exception:
                pass
        return samples_read / float(self.sample_rate)

    def blocksize_bytes(self) -> int:
        # int16 mono -> 2 bytes/sample
        return int(self.sample_rate * self.channels * 2 * 0.1)  # 100 ms 一块

    def stop(self) -> None:
        self._stop_event.set()
        self._running = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None


def create_audio_source(
    cfg: dict,
    video_path: Optional[str | Path] = None,
    timestamp_provider: Optional[Callable[[], float]] = None,
) -> Optional[AudioSource]:
    """根据配置创建音频源。

    ``timestamp_provider`` 仅在文件音轨模式下生效，用于把音频时间戳
    对齐到视频画面进度。
    """
    cfg = cfg or {}
    stype = cfg.get("type", "microphone")
    device_index = cfg.get("index")
    if device_index is not None:
        try:
            device_index = int(device_index)
        except Exception:
            device_index = None
    if stype == "file":
        if video_path is None:
            return None
        return FileAudioSource(
            video_path,
            realtime=True,
            speed=1.0,
            timestamp_provider=timestamp_provider,
        )
    if stype == "loopback":
        return LoopbackAudioSource(device_index=device_index)
    return MicrophoneAudioSource(device_index=device_index)
