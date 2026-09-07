"""角点标定对话框。

独立窗口，提供三种角点设置模式：
- 自动检测角点：单次运行 auto_detect，锁定结果
- 手动标定角点：用户在画面上点击 4 个角
- 逐帧自动跟踪（默认）：每帧自动检测，实时显示

录制前必须通过此对话框完成角点设置。
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import cv2
import numpy as np
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ..core.config import Config
from ..core.media_source import create_video_source
from ..core.screen_detector import QuadStabilizer, ScreenDetector
from .preview_widget import PreviewWidget


# ────────────────────────────────────────────── 预览工作线程
class CalibrationWorker(QThread):
    """标定专用预览线程，支持跟踪模式切换和当前帧获取。"""

    # dewarped 可能为 None (角点无效)，用 object 避免 emit 时类型错误
    frame_ready = pyqtSignal(object, object, float)
    camera_error = pyqtSignal(str)

    _MAX_CONSECUTIVE_FAILS = 150  # 约 5 秒无帧 → 报错

    def __init__(self, source_cfg: dict, resolution_mode: str = "max",
                 target_aspect: float = 16.0 / 9.0, parent=None):
        super().__init__(parent)
        self._source_cfg = dict(source_cfg)
        self._resolution_mode = resolution_mode
        self._target_aspect = float(target_aspect)
        self._stop_event = threading.Event()
        self._manual_corners: Optional[np.ndarray] = None
        self._tracking = True
        self._last_frame: Optional[np.ndarray] = None
        self._frame_lock = threading.Lock()
        self._source = None

    def set_corners(self, corners: Optional[np.ndarray]) -> None:
        self._manual_corners = corners

    def set_tracking(self, enabled: bool) -> None:
        self._tracking = enabled
        if enabled:
            self._manual_corners = None

    def get_last_frame(self) -> Optional[np.ndarray]:
        with self._frame_lock:
            return self._last_frame.copy() if self._last_frame is not None else None

    def run(self) -> None:
        source = create_video_source(self._source_cfg, self._resolution_mode)
        if source is None:
            self.camera_error.emit("无法打开视频源")
            return
        # 必须在 start() 之前赋值，否则 stop() 在 start 期间调用时
        # self._source 为 None → 无法打断 start 的重试循环 → QThread 崩溃
        self._source = source
        if not source.start():
            self.camera_error.emit("无法打开视频源")
            return
        det = ScreenDetector(target_aspect=self._target_aspect)
        stab = QuadStabilizer(alpha=0.25)
        last_emit = 0.0
        emit_interval = 0.1
        consecutive_fails = 0
        try:
            while not self._stop_event.is_set():
                ok, frame, ts = source.read_frame()
                if not ok or frame is None:
                    if source.is_file:
                        source.stop()
                        if not source.start():
                            break
                        continue
                    consecutive_fails += 1
                    if consecutive_fails >= self._MAX_CONSECUTIVE_FAILS:
                        self.camera_error.emit("摄像头持续无信号")
                        break
                    self.msleep(10)
                    continue

                consecutive_fails = 0

                with self._frame_lock:
                    self._last_frame = frame.copy()

                if self._manual_corners is not None:
                    corners = self._manual_corners.copy()
                elif self._tracking:
                    corners = det.auto_detect(frame)
                    corners = stab.update(corners, frame.shape)
                else:
                    corners = None

                now = time.perf_counter()
                if now - last_emit >= emit_interval:
                    last_emit = now
                    overlay = det.draw_overlay(frame, corners) if corners is not None else frame
                    dewarped = None
                    if corners is not None and det.is_valid_corners(corners, frame.shape):
                        dewarped = det.dewarp(frame, corners)
                    dewarped_show = (
                        dewarped if dewarped is not None
                        else np.zeros((180, 320, 3), dtype=np.uint8)
                    )
                    self.frame_ready.emit(overlay, dewarped_show, ts)
        except Exception as exc:
            print(f"[Calibration] error: {exc}")
            self.camera_error.emit(f"标定异常: {exc}")
        finally:
            source.stop()
            self._source = None

    def stop(self) -> None:
        self._stop_event.set()
        # 如果线程阻塞在 cv2.VideoCapture.read()，直接释放摄像头可打断阻塞
        if self._source is not None:
            try:
                self._source.stop()
            except Exception:
                pass
        self.wait(5000)


# ────────────────────────────────────────────── 标定对话框
class CornerCalibrationDialog(QDialog):
    """角点标定对话框，复用主预览流，支持自动检测 / 手动标定 / 逐帧跟踪三种模式。"""

    def __init__(self, config: Config, preview_worker, parent=None):
        super().__init__(parent)
        self.config = config
        self._worker = preview_worker  # 复用主预览的 PreviewWorker
        self._pending_corners: Optional[np.ndarray] = None
        self._mode = "tracking"  # "auto" | "manual" | "tracking"

        self.setWindowTitle("角点标定")
        self.setMinimumSize(720, 500)
        self._setup_ui()
        self._connect_signals()

    # ── UI 构建
    def _setup_ui(self):
        layout = QVBoxLayout(self)

        self.preview = PreviewWidget()
        self.preview.setMinimumSize(640, 360)
        # 标定时必须能看到完整画面才能框选四角，故缩放适配显示
        # （不要 1:1 原生像素，否则高分辨率原图会被 QLabel 裁掉）
        self.preview.set_native_size(False)
        layout.addWidget(self.preview, 1)

        self.status_label = QLabel("逐帧自动跟踪中...")
        self.status_label.setObjectName("lbl_hint")
        layout.addWidget(self.status_label)

        controls = QHBoxLayout()
        self.btn_auto_detect = QPushButton("自动检测角点")
        self.btn_manual = QPushButton("手动标定角点")
        self.btn_manual.setCheckable(True)
        self.btn_tracking = QPushButton("逐帧自动跟踪")
        self.btn_tracking.setCheckable(True)
        self.btn_tracking.setChecked(True)
        controls.addWidget(self.btn_auto_detect)
        controls.addWidget(self.btn_manual)
        controls.addWidget(self.btn_tracking)

        # 投影区域目标比例：决定矫正（拉直）输出的宽高比
        controls.addStretch()
        controls.addWidget(QLabel("目标比例"))
        saved_aspect = str(self.config.get("projection_aspect", "16:9"))
        self.btn_aspect_169 = QPushButton("16:9")
        self.btn_aspect_43 = QPushButton("4:3")
        for b in (self.btn_aspect_169, self.btn_aspect_43):
            b.setCheckable(True)
        self.btn_aspect_169.setChecked(saved_aspect != "4:3")
        self.btn_aspect_43.setChecked(saved_aspect == "4:3")
        controls.addWidget(self.btn_aspect_169)
        controls.addWidget(self.btn_aspect_43)
        layout.addLayout(controls)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.btn_confirm = QPushButton("确认")
        self.btn_confirm.setObjectName("btn_confirm")
        self.btn_confirm.setEnabled(True)  # tracking 模式默认可确认
        self.btn_cancel = QPushButton("取消")
        buttons.addWidget(self.btn_confirm)
        buttons.addWidget(self.btn_cancel)
        layout.addLayout(buttons)

    # ── 信号连接
    def _connect_signals(self):
        self.btn_auto_detect.clicked.connect(self._on_auto_detect)
        self.btn_manual.toggled.connect(self._on_manual_toggled)
        self.btn_tracking.toggled.connect(self._on_tracking_toggled)
        self.btn_aspect_169.toggled.connect(self._on_aspect_changed)
        self.btn_aspect_43.toggled.connect(self._on_aspect_changed)
        self.btn_confirm.clicked.connect(self._on_confirm)
        self.btn_cancel.clicked.connect(self.reject)
        self.preview.corners_changed.connect(self._on_corners_changed)
        # 复用主预览流的信号（不再自建 CalibrationWorker）
        if self._worker is not None:
            self._worker.frame_ready.connect(self._on_frame)
            self._worker.camera_error.connect(self._on_camera_error)

    # ── 目标比例
    def _current_aspect(self) -> float:
        return 4.0 / 3.0 if self.btn_aspect_43.isChecked() else 16.0 / 9.0

    def _on_aspect_changed(self, checked: bool) -> None:
        """保持 16:9 / 4:3 互斥选中。"""
        if not checked:
            # 保证至少一个处于选中状态
            if not self.btn_aspect_169.isChecked() and not self.btn_aspect_43.isChecked():
                self.btn_aspect_169.setChecked(True)
            return
        sender = self.sender()
        if sender is self.btn_aspect_169 and self.btn_aspect_43.isChecked():
            self.btn_aspect_43.blockSignals(True)
            self.btn_aspect_43.setChecked(False)
            self.btn_aspect_43.blockSignals(False)
        elif sender is self.btn_aspect_43 and self.btn_aspect_169.isChecked():
            self.btn_aspect_169.blockSignals(True)
            self.btn_aspect_169.setChecked(False)
            self.btn_aspect_169.blockSignals(False)
        self.status_label.setText(
            f"目标比例 {('4:3' if sender is self.btn_aspect_43 else '16:9')} — 矫正结果将按此比例铺满"
        )

    def _on_frame(self, overlay, dewarped, ts):
        self.preview.set_image(overlay)

    def _on_camera_error(self, msg: str):
        """标定摄像头不可用（如被占用/无信号）时给出明确提示，而非黑屏。"""
        self.status_label.setText(f"⚠ 无法获取预览画面：{msg}")
        self.btn_confirm.setEnabled(False)

    # ── 模式切换
    def _set_mode(self, mode):
        self._mode = mode
        if mode != "manual":
            self.btn_manual.setChecked(False)
            self.preview.allow_corner_selection(False)
        if mode != "tracking":
            self.btn_tracking.setChecked(False)

    # ── 自动检测（单次）
    def _on_auto_detect(self):
        frame = self._worker.get_last_frame() if self._worker else None
        if frame is None:
            self.status_label.setText("无可用画面")
            return
        det = ScreenDetector(target_aspect=self._current_aspect())
        corners = det.auto_detect(frame)
        if corners is not None and det.is_valid_corners(corners, frame.shape):
            self._pending_corners = corners
            self._set_mode("auto")
            if self._worker:
                self._worker.set_tracking(False)
                self._worker.set_corners(corners)
            self.status_label.setText("已检测到 4 个角点")
            self.btn_confirm.setEnabled(True)
        else:
            self.status_label.setText("未检测到有效角点，请手动标定")

    # ── 手动标定
    def _on_manual_toggled(self, checked):
        if checked:
            self._set_mode("manual")
            self.btn_tracking.setChecked(False)
            if self._worker:
                self._worker.set_tracking(False)
                self._worker.set_corners(None)
            self.preview.allow_corner_selection(True)
            self.status_label.setText("请在画面上依次点击屏幕的 4 个角")
            self.btn_confirm.setEnabled(False)
        else:
            self.preview.allow_corner_selection(False)

    # ── 逐帧跟踪
    def _on_tracking_toggled(self, checked):
        if checked:
            self._set_mode("tracking")
            self.btn_manual.setChecked(False)
            self.preview.allow_corner_selection(False)
            self._pending_corners = None
            if self._worker:
                self._worker.set_tracking(True)
                self._worker.set_corners(None)
            self.btn_confirm.setEnabled(True)
            self.status_label.setText("逐帧自动跟踪中...")
        else:
            if self._worker:
                self._worker.set_tracking(False)

    # ── 手动角点回调
    def _on_corners_changed(self, corners_list):
        if len(corners_list) == 4:
            self._pending_corners = np.array(corners_list, dtype=np.float32)
            if self._worker:
                self._worker.set_corners(self._pending_corners)
            self.btn_confirm.setEnabled(True)
            self.status_label.setText("已标定 4 个角点")

    # ── 确认 / 取消
    def _on_confirm(self):
        # 保存目标比例，预览/录制线程据此决定矫正输出宽高比
        self.config.set(
            "projection_aspect", "4:3" if self.btn_aspect_43.isChecked() else "16:9"
        )
        if self._mode == "tracking":
            self.config.set("manual_corners", None)
            self.config.save()
            self.accept()
        elif self._pending_corners is not None:
            corners_list = self._pending_corners.tolist()
            self.config.set("manual_corners", corners_list)
            self.config.save()
            self.accept()

    def get_confirmed_corners(self) -> Optional[np.ndarray]:
        if self._mode == "tracking":
            return None
        return self._pending_corners

    # ── 关闭（只断开信号，不停止主预览流）
    def _disconnect_worker(self):
        if self._worker is not None:
            try:
                self._worker.frame_ready.disconnect(self._on_frame)
            except (TypeError, RuntimeError):
                pass
            try:
                self._worker.camera_error.disconnect(self._on_camera_error)
            except (TypeError, RuntimeError):
                pass

    def closeEvent(self, event):
        self._disconnect_worker()
        event.accept()

    def reject(self):
        self._disconnect_worker()
        super().reject()

    def accept(self):
        self._disconnect_worker()
        super().accept()
