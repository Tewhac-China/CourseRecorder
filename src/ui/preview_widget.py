"""可点击的 OpenCV 预览控件。

继承 QLabel，在保持宽高比的前提下显示 BGR 图像，
并允许用户点击 4 个点以标定屏幕四角。

角点绘制规则：
- ``draw_interactive_corners=True``（手动标定模式）时，由控件自行绘制青绿色角点 + 半透明连线，
  同时接受鼠标点击输入。
- ``draw_interactive_corners=False``（录制中）时，角点由外部 overlay（ScreenDetector.draw_overlay）
  已经画在画面上，控件不再重复绘制。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

import cv2
import numpy as np
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QImage, QMouseEvent, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QLabel


class ScaledImageLabel(QLabel):
    """等比例填充整个控件区域显示 QPixmap 的标签。

    每次绘制时按当前控件尺寸做 letterbox（保持宽高比、居中、铺满可用空间），
    窗口缩放时画面随之自适应——普通 QLabel + scaled() 只在设置时缩放一次，
    之后控件变大/变小画面都不会跟随。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(120, 90)

    def sizeHint(self):
        from PyQt5.QtCore import QSize
        return QSize(320, 180)

    def minimumSizeHint(self):
        from PyQt5.QtCore import QSize
        return QSize(160, 90)

    def paintEvent(self, event) -> None:
        pix = self.pixmap()
        rect = self.contentsRect()
        if pix is None or pix.isNull() or rect.width() < 4 or rect.height() < 4:
            super().paintEvent(event)
            return
        scale = min(rect.width() / pix.width(), rect.height() / pix.height())
        disp_w = max(1, int(pix.width() * scale))
        disp_h = max(1, int(pix.height() * scale))
        scaled = pix.scaled(disp_w, disp_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        x = rect.x() + (rect.width() - scaled.width()) // 2
        y = rect.y() + (rect.height() - scaled.height()) // 2
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawPixmap(x, y, scaled)
        painter.end()


class PreviewWidget(QLabel):
    """支持鼠标点击标定四边形的预览控件。"""
    corners_changed = pyqtSignal(list)
    # 单点点选模式（手机摄像头对焦/测光区域）：发出"当前显示图像"坐标
    point_picked = pyqtSignal(float, float)

    def __init__(self, parent=None, max_corners: int = 4) -> None:
        super().__init__(parent)
        self.max_corners = int(max_corners)
        self._image: Optional[np.ndarray] = None
        self._raw_size: Tuple[int, int] = (0, 0)
        self._corners: List[Tuple[float, float]] = []  # 原始分辨率坐标
        self._allow_click = False
        self._draw_interactive_corners = False  # 仅在手动标定模式下为 True
        self._pick_mode = False  # 单点点选模式（对焦/测光区域）
        self._blackout = False  # 黑屏模式：忽略所有 set_image 调用
        self._native_size = True  # 1:1 原始像素显示（不做任何缩放）
        self._connecting = False  # 连接中覆盖层
        self._connect_timer_id = 0  # 动画定时器 ID
        self._connect_dots = 0      # 动画点数
        self.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.setMinimumSize(160, 120)
        self.setObjectName("preview_widget")

    def set_point_pick_mode(self, on: bool) -> None:
        """切换单点点选模式（用于指定对焦/测光区域）。"""
        self._pick_mode = bool(on)
        self.setCursor(Qt.CrossCursor if on else Qt.ArrowCursor)

    def set_native_size(self, native: bool) -> None:
        """切换是否按原始分辨率 1:1 显示（False 时缩放以适配控件）。"""
        self._native_size = bool(native)
        self.updateGeometry()
        self.update()

    def sizeHint(self):
        """原生模式下返回图像的真实像素尺寸，供 QScrollArea 决定滚动范围。"""
        if self._native_size and self._image is not None:
            h, w = self._image.shape[:2]
            from PyQt5.QtCore import QSize
            return QSize(int(w), int(h))
        from PyQt5.QtCore import QSize
        return QSize(320, 180)

    def allow_corner_selection(self, allow: bool) -> None:
        """切换手动标定模式。True 时接受点击并绘制角点。"""
        self._allow_click = bool(allow)
        self._draw_interactive_corners = bool(allow)
        self.setCursor(Qt.CrossCursor if allow else Qt.ArrowCursor)
        self.update()

    def set_image(self, bgr: np.ndarray) -> None:
        """设置待显示的 BGR 图像。黑屏模式下忽略。"""
        if self._blackout:
            return
        if bgr is None or bgr.size == 0:
            return
        self._image = bgr.copy()
        self._raw_size = (bgr.shape[1], bgr.shape[0])
        # 原生模式下通知布局尺寸变化，让外层 QScrollArea 更新滚动范围
        self.updateGeometry()
        self.update()

    def set_blackout(self) -> None:
        """进入黑屏模式：清空画面并拒绝后续 set_image 调用。"""
        self._blackout = True
        self._image = None
        self._raw_size = (0, 0)
        self._corners.clear()
        self.update()

    def clear_blackout(self) -> None:
        """退出黑屏模式，恢复接受 set_image 调用。"""
        self._blackout = False

    def set_connecting(self, connecting: bool) -> None:
        """切换"连接中"覆盖层（左下角显示等待动画）。"""
        if connecting == self._connecting:
            return
        self._connecting = connecting
        if connecting:
            self._connect_dots = 0
            self._connect_timer_id = self.startTimer(500)  # 每500ms刷新动画
        else:
            if self._connect_timer_id:
                self.killTimer(self._connect_timer_id)
                self._connect_timer_id = 0
        self.update()

    def timerEvent(self, event) -> None:
        if event.timerId() == self._connect_timer_id and self._connecting:
            self._connect_dots = (self._connect_dots + 1) % 4
            self.update()

    def get_corners(self) -> List[Tuple[float, float]]:
        return list(self._corners)

    def set_corners(self, corners: List[Tuple[float, float]]) -> None:
        self._corners = [(float(x), float(y)) for x, y in corners[: self.max_corners]]
        self.update()

    def clear_corners(self) -> None:
        self._corners.clear()
        self.update()

    def _to_image_coords(self, event: QMouseEvent) -> Tuple[float, float]:
        """把鼠标点击坐标映射回当前显示图像的像素坐标。"""
        img_w, img_h = self._raw_size
        if self._native_size:
            scale = 1.0
            offset_x = offset_y = 0.0
        else:
            widget_rect = self.contentsRect()
            scale = min(widget_rect.width() / img_w, widget_rect.height() / img_h)
            disp_w, disp_h = img_w * scale, img_h * scale
            offset_x = (widget_rect.width() - disp_w) / 2
            offset_y = (widget_rect.height() - disp_h) / 2
        x = (event.x() - offset_x) / scale
        y = (event.y() - offset_y) / scale
        return max(0.0, min(float(img_w), x)), max(0.0, min(float(img_h), y))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._raw_size[0] == 0:
            super().mousePressEvent(event)
            return
        # 单点点选模式（对焦/测光区域）：与角点选择相同的坐标映射
        if self._pick_mode:
            x, y = self._to_image_coords(event)
            self.point_picked.emit(x, y)
            return
        if not self._allow_click:
            super().mousePressEvent(event)
            return
        x, y = self._to_image_coords(event)
        if len(self._corners) >= self.max_corners:
            self._corners.clear()
        self._corners.append((x, y))
        self.corners_changed.emit(self.get_corners())
        self.update()

    def paintEvent(self, event) -> None:
        if self._image is None:
            super().paintEvent(event)
            return

        widget_rect = self.contentsRect()
        img_h, img_w = self._image.shape[:2]

        if self._native_size:
            # 1:1 原始像素：不做任何缩放，截取多大就显示多大
            disp_w, disp_h = img_w, img_h
            scale = 1.0
            rgb = cv2.cvtColor(self._image, cv2.COLOR_BGR2RGB)
            qimg = QImage(rgb.data, img_w, img_h, rgb.strides[0], QImage.Format_RGB888)
            pixmap = QPixmap.fromImage(qimg)
            x, y = 0, 0
        else:
            scale = min(widget_rect.width() / img_w, widget_rect.height() / img_h)
            disp_w, disp_h = int(img_w * scale), int(img_h * scale)
            resized = cv2.resize(self._image, (disp_w, disp_h),
                                 interpolation=cv2.INTER_LINEAR)
            rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
            qimg = QImage(rgb.data, disp_w, disp_h, rgb.strides[0], QImage.Format_RGB888)
            pixmap = QPixmap.fromImage(qimg)
            # 居中绘制
            x = (widget_rect.width() - disp_w) // 2
            y = (widget_rect.height() - disp_h) // 2

        painter = QPainter(self)
        painter.drawPixmap(x, y, pixmap)

        # 绘制角点（仅在手动标定模式下）
        if self._draw_interactive_corners and self._corners and self._raw_size[0] > 0:
            from PyQt5.QtGui import QColor
            accent = QColor("#00d4aa")
            accent_dim = QColor("#00d4aa80")
            pen = QPen(accent)
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(accent)
            pts = []
            for i, (cx, cy) in enumerate(self._corners):
                px = int(x + cx * scale)
                py = int(y + cy * scale)
                pts.append((px, py))
                painter.drawEllipse(px - 5, py - 5, 10, 10)
                painter.setPen(QPen(QColor("#e2e6f0")))
                painter.drawText(px + 8, py - 8, str(i + 1))
                painter.setPen(pen)
            # 连线
            if len(pts) > 1:
                pen.setColor(accent_dim)
                pen.setWidth(1)
                painter.setPen(pen)
                for i in range(len(pts)):
                    p1 = pts[i]
                    p2 = pts[(i + 1) % len(pts)]
                    painter.drawLine(p1[0], p1[1], p2[0], p2[1])

        # "连接中"覆盖层：左下角半透明背景 + 文字 + 动画省略号
        if self._connecting:
            from PyQt5.QtGui import QColor, QFont
            from PyQt5.QtCore import QRect
            dots = "." * self._connect_dots
            text = f"连接中{dots}"
            font = QFont()
            font.setPointSize(12)
            font.setBold(True)
            painter.setFont(font)
            fm = painter.fontMetrics()
            tw = fm.horizontalAdvance(text)
            th = fm.height()
            pad = 10
            rx = pad
            ry = widget_rect.height() - th - pad * 2
            # 半透明黑色背景
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 160))
            painter.drawRoundedRect(rx, ry, tw + pad * 2, th + pad, 6, 6)
            # 白色文字
            painter.setPen(QColor("#ffffff"))
            painter.drawText(rx + pad, ry + pad + fm.ascent(), text)

        painter.end()
