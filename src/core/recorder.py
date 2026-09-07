"""录制编排器。

把视频源、音频源、屏幕检测、幻灯片提取、ASR 转写串联起来，
并通过 Qt 信号把结果发回主界面。

关键设计：
- `stop()` 是**非阻塞**的：设标志后立即返回，实际清理在后台线程完成。
- 视频线程结束时通过 `_request_stop` 信号通知 UI 线程调用 `stop()`，
  避免在视频线程内自联（self-join deadlock）。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import cv2
import numpy as np
from PyQt5.QtCore import Qt, QObject, pyqtSignal

from ..app import MODELS_DIR, OUTPUTS_DIR
from .audio_source import AudioSource, create_audio_source
from .config import Config
from .media_source import MediaSource, NullSource, create_video_source
from .screen_detector import QuadStabilizer, ScreenDetector
from .session import Session
from .slide_extractor import SlideExtractor
from .transcriber import Transcriber
from .translator import Translator


class Recorder(QObject):
    """录制控制器。"""

    frame_updated = pyqtSignal(np.ndarray, np.ndarray, float)
    slide_captured = pyqtSignal(dict)
    transcript_updated = pyqtSignal(dict)
    state_changed = pyqtSignal(str)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    # 内部信号：视频线程结束时通知 UI 线程触发 stop
    _request_stop = pyqtSignal()

    def __init__(self, config: Config, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.config = config
        self._state = "idle"  # idle / starting / recording / stopping

        self._media_source: Optional[MediaSource] = None
        self._audio_source: Optional[AudioSource] = None
        self._session: Optional[Session] = None
        self._detector: Optional[ScreenDetector] = None
        self._stabilizer: Optional[QuadStabilizer] = None
        self._extractor: Optional[SlideExtractor] = None
        self._transcriber: Optional[Transcriber] = None
        self._translator: Optional[Translator] = None
        self._preloaded_translator: Optional[Translator] = None

        self._video_thread: Optional[threading.Thread] = None
        self._cleanup_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # 置位 = 已暂停
        self._camera_released = threading.Event()  # 置位 = 摄像头已让出（标定用）
        self._audio_paused = threading.Event()  # 置位 = 音频已暂停
        self._manual_corners: Optional[np.ndarray] = None
        self._use_manual_corners = False
        self._last_session_dir: Optional[str] = None

        # 最近一帧矫正画面，供"手动提取幻灯片"使用
        self._latest_dewarped: Optional[np.ndarray] = None
        self._latest_dewarped_ts: float = 0.0
        self._frame_lock = threading.Lock()

        # 当前视频帧时间戳（媒体时间轴）
        self._current_video_ts = 0.0
        self._ts_lock = threading.Lock()
        self._audio_is_file = False

        # 视频自然结束 → 通知 UI 线程触发 stop（避免在视频线程内自联）
        self._request_stop.connect(self.stop, type=Qt.QueuedConnection)

    def state(self) -> str:
        return self._state

    def set_preloaded_translator(self, translator: Optional[Translator]) -> None:
        """接收预加载的翻译器，录制时直接使用无需等待。"""
        self._preloaded_translator = translator

    # ------------------------------------------------------------ 时间同步
    def video_ts(self) -> float:
        with self._ts_lock:
            return self._current_video_ts

    def _set_video_ts(self, ts: float) -> None:
        with self._ts_lock:
            self._current_video_ts = float(ts)

    # ------------------------------------------------------------ 启动
    def start(
        self,
        video_source_cfg: Dict[str, Any],
        audio_source_cfg: Dict[str, Any],
    ) -> bool:
        if self._state in ("starting", "recording"):
            return False
        self._set_state("starting")
        self._stop_event.clear()
        self._pause_event.clear()
        self._audio_paused.clear()

        # 手动角点
        mc = self.config.get("manual_corners")
        if mc and len(mc) == 4:
            try:
                pts = np.asarray(mc, dtype=np.float32).reshape(4, 2)
                if ScreenDetector.is_valid_corners(pts):
                    self._manual_corners = pts
                    self._use_manual_corners = True
            except Exception:
                self._manual_corners = None
        else:
            self._manual_corners = None
            self._use_manual_corners = False

        # 视频源
        resolution_mode = self.config.get("camera_resolution_mode", "max")
        media = create_video_source(video_source_cfg, resolution_mode)
        if media is None:
            self.error.emit("无法创建视频源")
            self._set_state("idle")
            return False
        if not media.start():
            self.error.emit(f"无法打开视频源: {video_source_cfg}")
            self._set_state("idle")
            return False
        self._media_source = media

        # 会话目录
        output_dir = self.config.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        is_null = isinstance(media, NullSource)
        record_raw = self.config.get("record_raw_video", True) and not media.is_file and not is_null
        session = Session(output_dir, video_source_cfg, audio_source_cfg, record_raw)
        session.create()
        self._session = session

        # 音频源
        # 文件模式：直接用 FileSource 预提取的音轨，无需独立音频源
        self._audio_is_file = media.is_file
        if media.is_file and hasattr(media, 'has_audio') and media.has_audio():
            self._audio_source = None  # 音频在视频循环中直接获取
            print("[Recorder] 文件模式：使用内嵌音轨（音画同步）")
        else:
            video_path = video_source_cfg.get("path") if video_source_cfg.get("type") == "file" else None
            audio = create_audio_source(
                audio_source_cfg,
                video_path=video_path,
                timestamp_provider=self.video_ts,
            )
            if audio is not None:
                if not audio.start(self._on_audio):
                    print("[Recorder] 音频源启动失败，将只录制视频")
                    audio = None
            self._audio_source = audio

        # 屏幕检测 / 幻灯片提取 / 转写
        is_null_source = isinstance(media, NullSource)
        if is_null_source:
            # 不启用画面记录模式：跳过视频处理组件
            self._detector = None
            self._stabilizer = None
            self._extractor = None
        else:
            # dewarp_size 为 None 时表示原生尺寸：矫正输出沿用源帧分辨率，
            # 不强制缩放为 1080P（保持 1:1 原始像素）
            size = self.config.dewarp_size
            self._detector = ScreenDetector(
                output_size=size, target_aspect=self.config.projection_aspect_ratio
            )
            self._stabilizer = QuadStabilizer(alpha=0.25)
            self._extractor = SlideExtractor(
                session.slides_dir,
                sensitivity=self.config.sensitivity,
                cooldown_sec=float(self.config.get("slide_cooldown_sec", 2.0)),
                stable_sec=float(self.config.get("slide_stable_sec", 0.6)),
                min_persist_sec=float(self.config.get("slide_min_persist_sec", 1.0)),
                min_changed_area=float(self.config.get("slide_min_changed_area", 0.7)),
            )
        self._transcriber = Transcriber(
            backend=self.config.get("asr_backend", "whisper"),
            model_size=self.config.model_size,
            language=self.config.language,
            model_dir=MODELS_DIR,
            vad_mode=int(self.config.get("asr_vad_mode", 1)),
            min_speech_sec=float(self.config.get("asr_min_speech_sec", 1.0)),
        )
        if not self._transcriber.start(self._on_transcript):
            self.error.emit("ASR 模型加载失败")
            self._sync_cleanup()
            self._set_state("idle")
            return False

        # 翻译器（可选）：优先使用预加载的，否则新建并后台加载
        if self.config.get("translate_enabled", False):
            if self._preloaded_translator is not None and self._preloaded_translator.is_loaded:
                self._translator = self._preloaded_translator
                self._preloaded_translator = None  # 转移所有权
                print("[Recorder] 使用预加载的翻译模型")
            else:
                self._translator = Translator(
                    model_name=self.config.get("translate_model", "Qwen/Qwen3-1.7B"),
                    target_lang=self.config.get("translate_target_lang", "zh"),
                    quantize=self.config.get("translate_quantize", "int4"),
                    use_gpu=self.config.get("translate_use_gpu", True),
                )
                threading.Thread(target=self._translator.load, daemon=True).start()

        # 启动视频处理线程
        self._video_thread = threading.Thread(target=self._video_loop, daemon=True)
        self._video_thread.start()
        self._set_state("recording")
        return True

    # ------------------------------------------------------------ 状态管理
    def _set_state(self, state: str) -> None:
        self._state = state
        self.state_changed.emit(state)

    # ------------------------------------------------------------ 停止（非阻塞）
    def stop(self) -> None:
        """请求停止录制。立即返回，实际清理在后台线程完成。"""
        if self._state not in ("starting", "recording", "paused"):
            return
        self._pause_event.clear()  # 解除暂停，让视频线程能正常退出
        self._audio_paused.clear()  # 解除音频暂停
        self._set_state("stopping")
        self._stop_event.set()
        self._cleanup_thread = threading.Thread(target=self._do_cleanup, daemon=True)
        self._cleanup_thread.start()

    # ------------------------------------------------------------ 暂停 / 继续
    def pause(self) -> None:
        """暂停录制：关闭当前视频分段，暂停音频写入。"""
        if self._state != "recording":
            return
        self._pause_event.set()
        self._audio_paused.set()  # 暂停音频写入

        # 暂停视频写入
        if self._session is not None:
            self._session.pause_video_writer()
            self._session.pause_audio()

        self._set_state("paused")

    def resume(self) -> None:
        """继续录制：启动新的视频分段，恢复音频写入。"""
        if self._state != "paused":
            return
        self._camera_released.clear()
        self._pause_event.clear()
        self._audio_paused.clear()  # 恢复音频写入

        # 视频分段会在视频线程中自动启动（通过 first_frame 逻辑）
        if self._session is not None:
            self._session.resume_audio()

        self._set_state("recording")

    # ------------------------------------------------------------ 让出/恢复摄像头（标定用）
    def release_camera(self) -> None:
        """暂停期间让出摄像头供标定对话框使用。"""
        if self._state != "paused":
            return
        self._camera_released.set()
        if self._media_source is not None:
            self._media_source.stop()

    def acquire_camera(self) -> bool:
        """标定结束后重新获取摄像头。"""
        if self._state != "paused":
            return False
        if self._media_source is not None:
            ok = self._media_source.start()
            if not ok:
                print("[Recorder] 重新获取摄像头失败")
                return False
        self._camera_released.clear()
        return True

    # ------------------------------------------------------------ 手动抓拍
    def capture_slide_now(self) -> bool:
        if self._state not in ("starting", "recording", "paused"):
            self.error.emit('请先点击"开始录制"，再手动提取幻灯片')
            return False
        if self._session is None or self._extractor is None:
            self.error.emit("录制尚未就绪，无法提取幻灯片")
            return False

        with self._frame_lock:
            frame = None if self._latest_dewarped is None else self._latest_dewarped.copy()
            ts = self._latest_dewarped_ts

        if frame is None:
            self.error.emit(
                "还没有可用的矫正画面。请先：\n"
                "1. 点击工具栏\"手动标定\"按钮\n"
                "2. 在左侧预览画面上依次点击屏幕的 4 个角\n"
                "3. 确认右侧出现矫正后的幻灯片画面\n"
                "4. 再点击\"提取当前幻灯片\""
            )
            return False

        info = self._extractor.force_save(frame, ts)
        if info is None:
            self.error.emit("保存失败：画面为空或尺寸过小")
            return False

        self._session.write_slide(info)
        self.slide_captured.emit(info)
        return True

    def set_slide_sensitivity(self, value: int) -> None:
        """动态更新幻灯片检测灵敏度（录制中可调，立即生效）。"""
        if self._extractor is not None:
            self._extractor.set_sensitivity(value)

    def replace_slide(self, target_path: str) -> bool:
        """用当前矫正画面覆盖指定路径的幻灯片文件（自动备份以便撤回）。"""
        if self._state not in ("starting", "recording", "paused"):
            return False
        with self._frame_lock:
            frame = None if self._latest_dewarped is None else self._latest_dewarped.copy()
        if frame is None:
            self.error.emit("还没有可用的矫正画面")
            return False
        try:
            import cv2, shutil
            # 备份原文件（.bak），供撤回使用
            bak_path = target_path + ".bak"
            if os.path.exists(target_path) and not os.path.exists(bak_path):
                shutil.copy2(target_path, bak_path)
            cv2.imwrite(target_path, frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
            return True
        except Exception as exc:
            self.error.emit(f"覆盖幻灯片失败: {exc}")
            return False

    def undo_replace_slide(self, target_path: str) -> bool:
        """撤回覆盖：从 .bak 备份恢复原幻灯片。"""
        bak_path = target_path + ".bak"
        if not os.path.exists(bak_path):
            self.error.emit("没有可撤回的覆盖（备份不存在）")
            return False
        try:
            import shutil
            shutil.copy2(bak_path, target_path)
            os.remove(bak_path)
            return True
        except Exception as exc:
            self.error.emit(f"撤回覆盖失败: {exc}")
            return False

    def delete_slide(self, target_path: str, index: int) -> bool:
        """删除指定幻灯片文件并从列表中移除。"""
        if self._state not in ("starting", "recording", "paused"):
            return False
        try:
            if os.path.exists(target_path):
                os.remove(target_path)
            # 同时删除备份（如有）
            bak = target_path + ".bak"
            if os.path.exists(bak):
                os.remove(bak)
            # 从主窗口的幻灯片列表中移除（通过返回 True 让调用方处理）
            return True
        except Exception as exc:
            self.error.emit(f"删除幻灯片失败: {exc}")
            return False

    # ------------------------------------------------------------ 视频线程
    # 摄像头连续失败多少次后报错（约 10 秒 @ 30fps 读取频率）
    _CAMERA_FAIL_THRESHOLD = 300

    def _video_loop(self) -> None:
        media = self._media_source
        session = self._session
        if media is None or session is None:
            self.error.emit("录制组件未初始化")
            return

        # 不启用画面记录模式：仅保持线程存活，音频由独立回调处理
        is_null = isinstance(media, NullSource)
        if is_null:
            while not self._stop_event.is_set():
                if self._pause_event.is_set():
                    time.sleep(0.1)
                    continue
                media.read_frame()  # 内部会阻塞 0.1s
            return

        detector = self._detector
        stabilizer = self._stabilizer
        extractor = self._extractor
        if detector is None or stabilizer is None or extractor is None:
            self.error.emit("录制组件未初始化")
            return

        first_frame = True
        need_reinit_video = False  # 暂停恢复后需要重新初始化视频写入器
        last_ui_emit = 0.0
        ui_interval = 0.1  # UI 预览最多 10fps，避免信号队列堆积导致画面卡死
        consecutive_fails = 0
        while not self._stop_event.is_set():
            # 暂停状态下：仍读帧保持摄像头活跃，但跳过一切处理
            if self._pause_event.is_set():
                # 摄像头已让出给标定对话框，等待恢复
                if self._camera_released.is_set():
                    time.sleep(0.1)
                    continue
                try:
                    media.read_frame()  # 丢弃结果，只为消耗缓冲
                except Exception:
                    pass
                # 标记需要重新初始化视频
                need_reinit_video = True
                time.sleep(0.05)
                continue

            # 如果刚从暂停恢复，需要重新初始化视频写入器
            if need_reinit_video:
                need_reinit_video = False
                first_frame = True  # 触发重新初始化

            try:
                ok, frame, ts = media.read_frame()
            except Exception as exc:
                print(f"[Recorder] read_frame 异常: {exc}")
                ok, frame = False, None

            if not ok or frame is None:
                if media.is_file:
                    break  # 文件播放结束
                consecutive_fails += 1
                if consecutive_fails >= self._CAMERA_FAIL_THRESHOLD:
                    self.error.emit("摄像头持续无信号，录制已停止。请检查摄像头连接后重试。")
                    break
                time.sleep(0.01)
                continue

            consecutive_fails = 0

            self._set_video_ts(ts)

            if first_frame:
                # 检查是否需要恢复视频写入器（暂停后继续）
                if session._ffmpeg_proc is None and session.record_raw_video:
                    session.resume_video_writer(frame, media.get_fps())
                elif session._ffmpeg_proc is None:
                    session.init_video_writer(frame, media.get_fps())
                first_frame = False

            # 屏幕检测
            if self._use_manual_corners and self._manual_corners is not None:
                corners = self._manual_corners.copy()
            else:
                corners = detector.auto_detect(frame)
                corners = stabilizer.update(corners, frame.shape)

            dewarped = None
            if corners is not None and detector.is_valid_corners(corners, frame.shape):
                dewarped = detector.dewarp(frame, corners)
                with self._frame_lock:
                    self._latest_dewarped = dewarped.copy()
                    self._latest_dewarped_ts = float(ts)

            # 幻灯片提取
            if dewarped is not None:
                slide_info = extractor.process(dewarped, ts)
                if slide_info is not None:
                    session.write_slide(slide_info)
                    self.slide_captured.emit(slide_info)

            session.write_video_frame(frame)

            # 文件模式：直接从 FileSource 获取对应音频段
            if hasattr(media, 'get_audio_chunk'):
                chunk = media.get_audio_chunk(ts, duration=1.0 / media.get_fps())
                if chunk:
                    session.write_audio(chunk)
                    if self._transcriber is not None:
                        self._transcriber.feed_chunk(chunk, ts)

            # 限制 UI 更新频率，避免大量 numpy 数据堆积在 Qt 信号队列中
            now = time.perf_counter()
            if now - last_ui_emit >= ui_interval:
                last_ui_emit = now
                preview_dewarped = dewarped if dewarped is not None else np.zeros((180, 320, 3), dtype=np.uint8)
                overlay = detector.draw_overlay(frame, corners) if corners is not None else frame
                self.frame_updated.emit(overlay, preview_dewarped, ts)

        # 文件自然结束 → 通过信号通知 UI 线程触发 stop（非阻塞）
        if self._state == "recording":
            self._request_stop.emit()

    # ------------------------------------------------------------ 音频回调
    def _on_audio(self, pcm_bytes: bytes, timestamp: float) -> None:
        try:
            # 检查音频是否暂停
            if self._audio_paused.is_set():
                return  # 暂停期间不写入音频

            ts = self.video_ts() if self._audio_is_file else float(timestamp)
            if self._session is not None:
                self._session.write_audio(pcm_bytes)
            if self._transcriber is not None:
                self._transcriber.feed_chunk(pcm_bytes, ts)
        except Exception as exc:
            # 音频回调在 sounddevice 线程中，异常不能传播到 UI
            print(f"[Recorder] 音频回调异常: {exc}")

    # ------------------------------------------------------------ 转写回调
    def _on_transcript(self, segment: Dict[str, Any]) -> None:
        # 翻译（如果启用且模型已加载）
        if self._translator is not None and self._translator.is_loaded:
            try:
                translated = self._translator.translate(segment.get("text", ""))
                if translated:
                    segment = {**segment, "translated": translated}
            except Exception as exc:
                print(f"[Recorder] 翻译异常: {exc}")

        # 写入转写数据（包含翻译）
        if self._session is not None:
            self._session.write_transcript(segment)

        self.transcript_updated.emit(segment)

    # ------------------------------------------------------------ 后台清理线程
    def _do_cleanup(self) -> None:
        """在独立线程中执行所有清理工作，确保 UI 线程不被阻塞。"""
        try:
            # 1. 等待视频线程结束
            if self._video_thread is not None and self._video_thread.is_alive():
                self._video_thread.join(timeout=15)

            # 2. 停止音频源
            try:
                if self._audio_source is not None:
                    self._audio_source.stop()
            except Exception as exc:
                print(f"[Recorder] audio stop error: {exc}")
            self._audio_source = None

            # 3. 停止视频源
            try:
                if self._media_source is not None:
                    self._media_source.stop()
            except Exception as exc:
                print(f"[Recorder] media stop error: {exc}")
            self._media_source = None

            # 4. 停止转写器（可能耗时：flush 剩余音频 + whisper 推理）
            try:
                if self._transcriber is not None:
                    self._transcriber.stop()
            except Exception as exc:
                print(f"[Recorder] transcriber stop error: {exc}")
            self._transcriber = None

            # 4.5 释放翻译器
            try:
                if self._translator is not None:
                    self._translator.unload()
            except Exception as exc:
                print(f"[Recorder] translator unload error: {exc}")
            self._translator = None

            # 5. 保存会话文件（slides/transcript/notes）
            try:
                if self._session is not None:
                    session_dir = self._session.finalize()
                    self._last_session_dir = session_dir
            except Exception as exc:
                print(f"[Recorder] finalize error: {exc}")
            self._session = None

        except Exception as exc:
            print(f"[Recorder] cleanup unexpected error: {exc}")
        finally:
            # 无论发生什么，都要回到 idle 状态并通知 UI
            self._detector = None
            self._stabilizer = None
            self._extractor = None
            self._video_thread = None
            with self._frame_lock:
                self._latest_dewarped = None
                self._latest_dewarped_ts = 0.0
            self._set_state("idle")
            self.finished.emit()

    # ------------------------------------------------------------ 同步清理（启动失败用）
    def _sync_cleanup(self) -> None:
        """启动阶段的同步清理（不走后台线程，因为此时录制还没真正开始）。"""
        self._stop_event.set()
        try:
            if self._audio_source is not None:
                self._audio_source.stop()
        except Exception:
            pass
        try:
            if self._media_source is not None:
                self._media_source.stop()
        except Exception:
            pass
        try:
            if self._transcriber is not None:
                self._transcriber.stop()
        except Exception:
            pass
        try:
            if self._translator is not None:
                self._translator.unload()
        except Exception:
            pass
        try:
            if self._session is not None:
                self._session.finalize()
        except Exception:
            pass
        self._audio_source = None
        self._media_source = None
        self._transcriber = None
        self._translator = None
        self._session = None
        self._detector = None
        self._stabilizer = None
        self._extractor = None

    def current_session_dir(self) -> Optional[str]:
        if self._session is not None:
            return str(self._session.dir)
        return self._last_session_dir
