"""模型加载串行锁。

ASR(SenseVoice/Whisper) 与翻译模型(Qwen3) 都会占用大量显存/CPU。
若二者同时加载（例如启动时后台预加载翻译模型，用户又立刻点录制），
会出现资源竞争导致其中一方加载失败（表现为"ASR 模型加载失败"、
录制无法开始）。这里提供一个共享锁，让两个模型的加载串行进行。
"""

from __future__ import annotations

import threading

_MODEL_LOAD_LOCK = threading.RLock()


def model_load_lock() -> threading.RLock:
    """返回模型加载共享锁（可重入，避免同一线程内嵌套加锁死锁）。"""
    return _MODEL_LOAD_LOCK
