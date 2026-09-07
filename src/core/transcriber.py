"""本地 ASR 实时转写（支持 Whisper / FunASR SenseVoice 双引擎）。

统一接口：
- 持续接收 16-bit 16kHz mono PCM
- 用 VAD 判断语音起止
- 语音段结束后送入 ASR 转写
- 返回带时间戳的文本段
"""

from __future__ import annotations

import collections
import io
import re
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import webrtcvad

Segment = dict  # {"start": float, "end": float, "text": str}
TranscriptCallback = Callable[[Segment], None]

_SAMPLE_RATE = 16000
_FRAME_MS = 30
_FRAME_BYTES = int(_SAMPLE_RATE * 2 * (_FRAME_MS / 1000.0))  # 960 bytes
_MIN_SPEECH_SEC = 1.0
_MAX_BUFFER_SEC = 10.0
_SILENCE_SEC_TO_CUT = 0.6


# ================================================================ 模型发现
_FUNASR_CACHE_FILE = Path.home() / ".course_recorder" / ".funasr_ok"


def _check_funasr_cached() -> bool:
    """检测 funasr 是否可导入，结果缓存到文件避免每次等待 ~18s。

    缓存文件包含 Python 解释器路径，更换环境后自动失效。
    删除 ~/.course_recorder/.funasr_ok 可强制重新检测。
    """
    cache = _FUNASR_CACHE_FILE
    try:
        if cache.exists():
            content = cache.read_text(encoding="utf-8").strip()
            if content.startswith("OK:"):
                cached_exe = content[3:]
                if cached_exe == sys.executable:
                    return True
            elif content == "FAIL:" + sys.executable:
                return False
    except Exception:
        pass

    # 缓存未命中或已过期，执行子进程检测
    funasr_ok = False
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import funasr; print('OK')"],
            capture_output=True, text=True, timeout=60,
        )
        funasr_ok = (result.returncode == 0 and "OK" in result.stdout)
    except Exception:
        pass

    # 写缓存
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tag = "OK" if funasr_ok else "FAIL"
        cache.write_text(f"{tag}:{sys.executable}", encoding="utf-8")
    except Exception:
        pass

    return funasr_ok


def _cuda_available() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False


def discover_models() -> List[Dict]:
    """扫描已下载的 ASR 模型，返回 [{backend, name, label, size_mb, downloaded}]。"""
    results: List[Dict] = []

    # --- Whisper 模型 ---
    whisper_cache = Path.home() / ".cache" / "whisper"
    whisper_local = Path(__file__).resolve().parents[2] / "models"
    whisper_sizes = {
        "tiny": 75, "base": 139, "small": 466, "medium": 1432, "large": 2871,
        "large-v2": 2871, "large-v3": 2871, "large-v3-turbo": 1550,
    }
    seen = set()
    for d in [whisper_local, whisper_cache]:
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.suffix == ".pt" and f.stem not in seen:
                name = f.stem
                seen.add(name)
                size_mb = f.stat().st_size // 1024 // 1024
                label = f"Whisper {name} ({size_mb}MB)"
                if name == "base":
                    label += " [当前]"
                results.append({
                    "backend": "whisper",
                    "name": name,
                    "label": label,
                    "size_mb": size_mb,
                    "downloaded": True,
                    "path": str(f),
                })

    # --- FunASR SenseVoice ---
    # 用子进程检测 funasr 是否可用，避免 sentencepiece .pyd segfault 崩溃主进程
    # 结果缓存到文件，避免每次启动都等待 ~18s 的 import 检测
    funasr_ok = _check_funasr_cached()

    if funasr_ok:
        try:
            funasr_cache = Path.home() / ".cache" / "funasr"
            modelscope_cache = Path.home() / ".cache" / "modelscope"
            sv_found = False
            for cache_dir in [funasr_cache, modelscope_cache]:
                if not cache_dir.is_dir():
                    continue
                for p in cache_dir.rglob("*SenseVoice*"):
                    if p.is_dir() and (any(p.glob("*.onnx")) or any(p.glob("*.pt")) or any(p.glob("*.bin"))):
                        sv_found = True
                        break
                if sv_found:
                    break
            if sv_found:
                results.append({
                    "backend": "sensevoice",
                    "name": "SenseVoiceSmall",
                    "label": "FunASR SenseVoice (中文优化, ~254MB) [推荐]",
                    "size_mb": 254,
                    "downloaded": True,
                })
            else:
                results.append({
                    "backend": "sensevoice",
                    "name": "SenseVoiceSmall",
                    "label": "FunASR SenseVoice (未下载, 安装命令见设置)",
                    "size_mb": 254,
                    "downloaded": False,
                })
        except Exception:
            pass
    else:
        results.append({
            "backend": "sensevoice",
            "name": "SenseVoiceSmall",
            "label": "FunASR SenseVoice (依赖损坏, 见修复命令)",
            "size_mb": 254,
            "downloaded": False,
        })

    return results


# ================================================================ 通用 VAD + 缓冲
class _VadBuffer:
    """VAD + 音频缓冲 + 切分逻辑，供各后端共用。"""

    def __init__(self, vad_mode: int = 1, min_speech_sec: float = _MIN_SPEECH_SEC) -> None:
        self.vad_mode = max(0, min(3, int(vad_mode)))
        self.min_speech_sec = max(0.1, float(min_speech_sec))
        self._vad = webrtcvad.Vad(self.vad_mode)
        self._lock = threading.Lock()
        self._buffer: collections.deque = collections.deque()
        self._buffer_duration = 0.0

    def feed(self, pcm_bytes: bytes, timestamp: float) -> None:
        with self._lock:
            self._buffer.append((float(timestamp), bytes(pcm_bytes)))
            self._buffer_duration += len(pcm_bytes) / (_SAMPLE_RATE * 2)

    def try_cut(self) -> Optional[Tuple[bytes, float, float]]:
        """尝试从缓冲区切出一段语音。返回 (pcm, start_ts, end_ts) 或 None。

        正向扫描：找到第一个足够长的静音段后的语音起始点，在该处切分。
        让转写尽早开始，避免音频在缓冲区中堆积。
        """
        with self._lock:
            if self._buffer_duration < self.min_speech_sec:
                return None
            frames = self._to_vad_frames()
            if not frames:
                return None
            silence_frames_needed = int(_SILENCE_SEC_TO_CUT * 1000 / _FRAME_MS)
            # 正向扫描：找到第一个满足阈值的静音→语音转换点
            silence_run = 0
            for i, (_, is_speech) in enumerate(frames):
                if is_speech:
                    if silence_run >= silence_frames_needed:
                        result = self._flush_upto(i)
                        if result is not None:
                            return result
                        # 切出的段太短，跳过这个切点继续扫描
                    silence_run = 0
                else:
                    silence_run += 1
            # 末尾全是静音：在最后语音帧处切
            if silence_run >= silence_frames_needed:
                cut_idx = len(frames) - silence_run
                if cut_idx > 0:
                    result = self._flush_upto(cut_idx)
                    if result is not None:
                        return result
            # 无合适切点或切点无效：缓冲区过大时强制切
            if self._buffer_duration > _MAX_BUFFER_SEC:
                return self._flush_all()
            return None

    def flush(self) -> Optional[Tuple[bytes, float, float]]:
        with self._lock:
            return self._flush_all()

    def _flush_all(self) -> Optional[Tuple[bytes, float, float]]:
        return self._flush_upto(None)

    def _flush_upto(self, frame_cut_idx: Optional[int]) -> Optional[Tuple[bytes, float, float]]:
        if not self._buffer:
            return None
        data = b"".join(b for _, b in self._buffer)
        if frame_cut_idx is not None:
            byte_cut = frame_cut_idx * _FRAME_BYTES
            speech_data = data[:byte_cut]
            remain_data = data[byte_cut:]
        else:
            speech_data = data
            remain_data = b""
        if len(speech_data) < int(self.min_speech_sec * _SAMPLE_RATE * 2):
            return None
        start_ts = self._buffer[0][0]
        duration = len(speech_data) / (_SAMPLE_RATE * 2)
        end_ts = start_ts + duration
        self._buffer.clear()
        if remain_data:
            self._buffer.append((end_ts, remain_data))
            self._buffer_duration = len(remain_data) / (_SAMPLE_RATE * 2)
        else:
            self._buffer_duration = 0.0
        return speech_data, start_ts, end_ts

    def _to_vad_frames(self) -> List[Tuple[bytes, bool]]:
        data = b"".join(b for _, b in self._buffer)
        frames = []
        n = len(data) // _FRAME_BYTES
        for i in range(n):
            frame = data[i * _FRAME_BYTES : (i + 1) * _FRAME_BYTES]
            try:
                is_speech = self._vad.is_speech(frame, _SAMPLE_RATE)
            except Exception:
                is_speech = False
            frames.append((frame, is_speech))
        return frames


# ================================================================ Whisper 后端
class _WhisperBackend:
    def __init__(self, model_size: str, device: str, language: Optional[str], model_dir: Optional[Path]) -> None:
        self.model_size = model_size
        self.device = device
        self.language = language
        self.model_dir = model_dir
        self._model = None

    def load(self) -> bool:
        if self._model is not None:
            return True
        try:
            import os
            import whisper
            print(f"[Whisper] 加载模型: {self.model_size} ({self.device})")

            # 绕过系统代理（避免 SSL 握手问题导致模型加载卡死）
            old_no_proxy = os.environ.get("no_proxy", "")
            os.environ["no_proxy"] = "*"
            os.environ["NO_PROXY"] = "*"

            if self.model_dir:
                self.model_dir.mkdir(parents=True, exist_ok=True)
                # 如果本地已有模型文件，直接加载避免联网
                local_pt = self.model_dir / f"{self.model_size}.pt"
                if local_pt.exists():
                    print(f"[Whisper] 从本地加载: {local_pt}")
                    self._model = whisper.load_model(
                        self.model_size, device=self.device,
                        download_root=str(self.model_dir),
                    )
                else:
                    self._model = whisper.load_model(
                        self.model_size, device=self.device,
                        download_root=str(self.model_dir),
                    )
            else:
                self._model = whisper.load_model(self.model_size, device=self.device)
            return True
        except Exception as exc:
            print(f"[Whisper] 加载失败: {exc}")
            return False

    # Whisper 要求最少 0.1s（1600 采样点），太短会触发 key/value 维度不匹配
    _MIN_SAMPLES = 1600

    def transcribe(self, pcm_float32: np.ndarray) -> str:
        if self._model is None:
            return ""
        if len(pcm_float32) < self._MIN_SAMPLES:
            return ""
        try:
            kwargs = {
                "fp16": (self.device == "cuda"),
                "condition_on_previous_text": False,
                "temperature": 0.0,
            }
            if self.language:
                kwargs["language"] = self.language
            result = self._model.transcribe(pcm_float32, **kwargs)
            return str(result.get("text", ""))
        except Exception as exc:
            print(f"[Whisper] 转写失败: {exc}")
            return ""


# ================================================================ FunASR SenseVoice 后端
class _SenseVoiceBackend:
    # SenseVoice 输出中的语言标签
    _LANG_TAG_RE = re.compile(r"<\|([a-z]{2})\|>")
    # 支持的语言过滤集合
    _VALID_LANGS = {"zh", "en", "ja", "ko", "yue"}

    def __init__(self, device: str, language: Optional[str]) -> None:
        self.device = device
        self.language = language
        self._model = None
        # 解析语言过滤：None/"auto"=不过滤, "zh+en"=仅中英, 具体语言=强制
        if language is None or language == "auto":
            self._filter_langs: Optional[set] = None
        elif "+" in language:
            self._filter_langs = set(language.split("+"))
        elif language in self._VALID_LANGS:
            self._filter_langs = {language}
        else:
            self._filter_langs = None

    def load(self) -> bool:
        if self._model is not None:
            return True
        try:
            import os
            # 绕过系统代理（避免 SSL 握手问题导致模型加载卡死）
            os.environ["no_proxy"] = "*"
            os.environ["NO_PROXY"] = "*"

            from funasr import AutoModel
            print(f"[SenseVoice] 加载模型 ({self.device})")
            self._model = AutoModel(
                model="iic/SenseVoiceSmall",
                vad_model="fsmn-vad",
                vad_kwargs={"max_single_segment_time": 30000},
                device=self.device,
                disable_update=True,
            )
            return True
        except Exception as exc:
            print(f"[SenseVoice] 加载失败: {exc}")
            return False

    def transcribe(self, pcm_float32: np.ndarray) -> str:
        if self._model is None:
            return ""
        try:
            res = self._model.generate(
                input=pcm_float32,
                cache={},
                language=None,
                use_itn=True,
                batch_size_s=60,
            )
            if res and len(res) > 0:
                raw = res[0].get("text", "")
                # 1) 语言标签过滤
                m = self._LANG_TAG_RE.search(raw)
                detected_lang = m.group(1) if m else "unknown"
                if self._filter_langs and detected_lang not in self._filter_langs:
                    return ""
                # 2) 去掉所有 <|xx|> 标签
                text = re.sub(r"<\|[^>]+\|>", "", raw).strip()
                # 3) 文本级过滤：非目标语言的字符清除
                if self._filter_langs:
                    text = self._filter_non_target_chars(text)
                return text
            return ""
        except Exception as exc:
            print(f"[SenseVoice] 转写失败: {exc}")
            return ""

    def _filter_non_target_chars(self, text: str) -> str:
        """清除不属于目标语言的字符（如日文假名、韩文等）。"""
        if not self._filter_langs or not text:
            return text
        # 日文假名（平假名 + 片假名）
        if "ja" not in self._filter_langs:
            text = re.sub(r"[぀-ゟ゠-ヿㇰ-ㇿ]+", " ", text)
        # 韩文
        if "ko" not in self._filter_langs:
            text = re.sub(r"[가-힯ᄀ-ᇿ㄰-㆏]+", " ", text)
        # 粤语（粤拼等保留，只清理声调标记）
        if "yue" not in self._filter_langs:
            pass  # 粤语与中文共用汉字，不做字符级过滤
        # 清理多余空格
        text = re.sub(r"\s+", " ", text).strip()
        return text


# ================================================================ 统一转写器
class Transcriber:
    """统一 ASR 转写器，支持 whisper / sensevoice 后端。"""

    def __init__(
        self,
        backend: str = "whisper",
        model_size: str = "base",
        device: Optional[str] = None,
        language: Optional[str] = None,
        model_dir: Optional[str | Path] = None,
        vad_mode: int = 1,
        min_speech_sec: float = _MIN_SPEECH_SEC,
    ) -> None:
        self.backend_name = backend
        self.device = device or ("cuda" if _cuda_available() else "cpu")
        self.language = language

        self._vad_buf = _VadBuffer(vad_mode=vad_mode, min_speech_sec=min_speech_sec)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._callback: Optional[TranscriptCallback] = None
        self._all_segments: List[Segment] = []

        # 创建后端
        if backend == "sensevoice":
            self._backend = _SenseVoiceBackend(device=self.device, language=language)
        else:
            self._backend = _WhisperBackend(
                model_size=model_size, device=self.device,
                language=language, model_dir=Path(model_dir) if model_dir else None,
            )

    def load_model(self) -> bool:
        return self._backend.load()

    def start(self, callback: Optional[TranscriptCallback] = None) -> bool:
        if not self.load_model():
            return False
        self._callback = callback
        self._running = True
        self._thread = threading.Thread(target=self._process_loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> List[Segment]:
        self._running = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=8)
        result = self._vad_buf.flush()
        if result:
            pcm, start_ts, end_ts = result
            self._do_transcribe(pcm, start_ts, end_ts)
        return list(self._all_segments)

    def feed_chunk(self, pcm_bytes: bytes, timestamp: float) -> None:
        self._vad_buf.feed(pcm_bytes, timestamp)

    def _process_loop(self) -> None:
        while self._running:
            time.sleep(0.2)
            result = self._vad_buf.try_cut()
            if result:
                pcm, start_ts, end_ts = result
                threading.Thread(
                    target=self._do_transcribe,
                    args=(pcm, start_ts, end_ts),
                    daemon=True,
                ).start()

    def _do_transcribe(self, pcm: bytes, start_ts: float, end_ts: float) -> None:
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        text = self._backend.transcribe(audio)
        seg: Segment = {"start": float(start_ts), "end": float(end_ts), "text": text.strip()}
        if seg["text"]:
            self._all_segments.append(seg)
            if self._callback is not None:
                try:
                    self._callback(seg)
                except Exception:
                    pass

    def get_segments(self) -> List[Segment]:
        return list(self._all_segments)
