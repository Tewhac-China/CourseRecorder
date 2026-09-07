"""配置读写：JSON 持久化到 ~/.course_recorder/config.json。"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from ..app import CONFIG_PATH, OUTPUTS_DIR, USER_DIR

DEFAULTS: Dict[str, Any] = {
    # {"type": "camera"|"file", "index": int, "id": str|None, "name": str|None,
    #  "path": str (file 模式),
    #  "max_width": int, "max_height": int (摄像头最高分辨率)}
    "video_source": {"type": "camera", "index": 0, "id": None, "name": None, "path": "",
                     "max_width": 0, "max_height": 0},
    # 摄像头分辨率模式："max"=最高分辨率, "1080p"=降采样到1080P
    "camera_resolution_mode": "max",
    # 投影区域目标比例："16:9" 或 "4:3"，决定矫正（拉直）输出比例
    "projection_aspect": "16:9",
    # {"type": "microphone"|"loopback"|"file", "id": str|None, "name": str|None}
    "audio_source": {"type": "microphone", "id": None, "name": None},
    # 幻灯片检测灵敏度 0(迟钝) ~ 100(敏感)
    "slide_sensitivity": 60,
    # 两张幻灯片之间的最小间隔（秒）
    "slide_cooldown_sec": 2.0,
    # 新幻灯片候选需要稳定多久才保存（秒）
    "slide_stable_sec": 0.6,
    # 新幻灯片候选至少持续多久（秒）
    "slide_min_persist_sec": 1.0,
    # 判定为"全局变化"所需的网格变化面积占比（过滤演讲者局部遮挡）
    "slide_min_changed_area": 0.7,
    # ASR
    "asr_backend": "sensevoice",        # whisper / sensevoice
    "asr_model_size": "SenseVoiceSmall", # tiny / base / small / medium (whisper) / SenseVoiceSmall
    "asr_language": "auto",             # auto / zh+en / zh / en / ja ...
    "asr_use_gpu": True,
    "asr_window_sec": 4.0,             # 每次送进 whisper 的音频窗口长度
    "asr_vad_mode": 1,                 # 0-3, 越小越不容易把噪音当语音
    "asr_min_speech_sec": 1.0,         # 低于此长度的语音不转写
    # 翻译
    "translate_enabled": False,            # 是否启用实时翻译
    "translate_model": "Qwen/Qwen3-1.7B",  # 翻译模型名称
    "translate_target_lang": "zh",         # 目标语言：zh / en / ja / ko
    "translate_quantize": "int4",          # 量化方式：int4 / int8 / fp16
    "translate_use_gpu": True,             # GPU 加速（显存不足时自动降级）
    "translate_color": "#888888",          # 翻译文本颜色（默认灰色）
    # 输出
    "output_dir": str(OUTPUTS_DIR),
    "record_raw_video": True,
    # 矫正画面输出尺寸: True=沿用源帧原始分辨率(1:1, 不做任何缩放);
    # False=强制缩放为下面的 dewarp_width x dewarp_height
    "dewarp_native_size": True,
    "dewarp_width": 1920,
    "dewarp_height": 1080,
    # 视频文件源：是否按实时速度播放（保证音视频时间轴对齐）
    "realtime_playback": True,
    "playback_speed": 1.0,
    # 手动标定的角点（原始分辨率坐标系），null 表示使用自动检测
    "manual_corners": None,
    # 最近一次选择的视频文件
    "last_video_file": "",
}

_MODEL_SIZES = ("tiny", "base", "small", "medium", "large")
_LANGUAGES = ("auto", "en", "zh", "ja", "ko", "fr", "de", "es", "ru")


class Config:
    """带默认值回退的字典式配置对象。"""

    def __init__(self, data: Dict[str, Any] | None = None, path: Path | None = None):
        self._path = Path(path) if path else Path(CONFIG_PATH)
        self._data: Dict[str, Any] = dict(DEFAULTS)
        if data:
            self._data.update(data)
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 读写
    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        target = Path(path) if path else Path(CONFIG_PATH)
        data: Dict[str, Any] = {}
        try:
            if target.exists():
                with target.open("r", encoding="utf-8") as fh:
                    parsed = json.load(fh)
                if isinstance(parsed, dict):
                    data = parsed
        except Exception as exc:  # 配置损坏时回退到默认值，不能阻止启动
            print(f"[Config] 读取配置失败，使用默认值: {exc}")
        return cls(data, target)

    def save(self) -> None:
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(self._path.suffix + ".tmp")
                with tmp.open("w", encoding="utf-8") as fh:
                    json.dump(self._data, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, self._path)
            except Exception as exc:
                print(f"[Config] 保存配置失败: {exc}")

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    # ------------------------------------------------------------ 访问
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            if key in self._data:
                return self._data[key]
            return DEFAULTS.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def update(self, mapping: Dict[str, Any]) -> None:
        with self._lock:
            self._data.update(mapping)

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    # ------------------------------------------------------------ 便捷属性
    @property
    def path(self) -> Path:
        return self._path

    @property
    def video_source(self) -> Dict[str, Any]:
        v = self.get("video_source")
        return dict(v) if isinstance(v, dict) else dict(DEFAULTS["video_source"])

    @property
    def audio_source(self) -> Dict[str, Any]:
        v = self.get("audio_source")
        return dict(v) if isinstance(v, dict) else dict(DEFAULTS["audio_source"])

    @property
    def output_dir(self) -> Path:
        raw = self.get("output_dir") or str(OUTPUTS_DIR)
        try:
            p = Path(raw)
            if not p.is_absolute():
                p = Path(__file__).resolve().parents[2] / p
            return p
        except Exception:
            return Path(OUTPUTS_DIR)

    @property
    def dewarp_size(self) -> Optional[tuple]:
        """矫正输出尺寸。原生模式（默认）返回 None，表示沿用源帧分辨率。"""
        if bool(self.get("dewarp_native_size", True)):
            return None
        return (int(self.get("dewarp_width", 1920)), int(self.get("dewarp_height", 1080)))

    @property
    def projection_aspect_ratio(self) -> float:
        """投影区域目标长宽比（用于矫正输出尺寸与角点检测偏好）。

        取值来自配置 ``projection_aspect``（"16:9" / "4:3" 等），解析失败回退 16:9。
        """
        raw = str(self.get("projection_aspect", "16:9")).strip().replace("：", ":")
        # 兼容 "16:9" 与 "16/9" 两种写法（存储用冒号）
        for sep in (":", "/"):
            if sep in raw:
                try:
                    num, den = raw.split(sep)
                    ratio = float(num) / float(den)
                    if ratio > 0:
                        return ratio
                except Exception:
                    break
        return 16.0 / 9.0

    @property
    def model_size(self) -> str:
        size = str(self.get("asr_model_size", "base")).lower()
        return size if size in _MODEL_SIZES else "base"

    @property
    def language(self) -> str | None:
        lang = str(self.get("asr_language", "auto")).lower()
        return None if lang in ("auto", "", "none") else lang

    @property
    def sensitivity(self) -> int:
        try:
            v = int(self.get("slide_sensitivity", 60))
        except Exception:
            v = 60
        return max(0, min(100, v))


def config_dir() -> Path:
    return Path(USER_DIR)
