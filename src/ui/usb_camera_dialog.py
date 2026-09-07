"""USB 摄像头配置对话框 — 通过 OpenCV 属性控制亮度/对比度/饱和度/色调/曝光。

自动探测摄像头支持的属性及范围，不支持的属性显示为灰色不可用。
"""

from __future__ import annotations

import threading
import time
from typing import Optional, Tuple

import cv2
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
)


def _probe_camera_props(index: int, backend: int = cv2.CAP_DSHOW) -> dict:
    """探测摄像头支持的属性及范围。

    返回格式: {prop_name: {"supported": bool, "min": float, "max": float, "current": float}}
    """
    cap = cv2.VideoCapture(index, backend)
    if cap is None or not cap.isOpened():
        return {}

    # warmup
    for _ in range(3):
        cap.read()

    # 定义要探测的属性（名称, OpenCV 常量, 默认测试范围）
    props_to_probe = [
        ("brightness", cv2.CAP_PROP_BRIGHTNESS, list(range(0, 256, 16))),
        ("contrast", cv2.CAP_PROP_CONTRAST, list(range(0, 256, 16))),
        ("saturation", cv2.CAP_PROP_SATURATION, list(range(0, 256, 16))),
        ("hue", cv2.CAP_PROP_HUE, list(range(-180, 181, 30))),
        ("exposure", cv2.CAP_PROP_EXPOSURE, list(range(-13, 1))),
        ("focus", cv2.CAP_PROP_FOCUS, [0, 1, 5, 10]),
        ("gain", cv2.CAP_PROP_GAIN, [0, 50, 100, 200]),
        ("zoom", cv2.CAP_PROP_ZOOM, [0, 50, 100, 200]),
    ]

    result = {}
    for name, prop, test_values in props_to_probe:
        current = cap.get(prop)
        # 尝试设置一个测试值来检测是否可写
        test_val = test_values[len(test_values) // 2] if test_values else 0
        ok = cap.set(prop, test_val)
        actual_after = cap.get(prop)
        supported = ok and abs(actual_after - current) > 0.01

        if supported:
            # 探测实际范围
            min_val, max_val = current, current
            for v in test_values:
                cap.set(prop, v)
                a = cap.get(prop)
                if abs(a - v) < 0.01:  # 值被接受了
                    min_val = min(min_val, a)
                    max_val = max(max_val, a)
            # 恢复原值
            cap.set(prop, current)
            result[name] = {
                "supported": True,
                "min": min_val,
                "max": max_val,
                "current": current,
                "prop": prop,
            }
        else:
            result[name] = {
                "supported": False,
                "min": 0,
                "max": 0,
                "current": current,
                "prop": prop,
            }

    cap.release()
    return result


class UsbCameraDialog(QDialog):
    """USB 摄像头参数配置面板。"""

    def __init__(self, camera_index: int = 0, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._camera_index = camera_index
        self._props: dict = {}
        self._updating = False  # 防止回写循环
        self._last_set_ms = 0.0

        self.setWindowTitle("USB 摄像头设置")
        self.setMinimumWidth(420)
        self._build_ui_empty()

        # 后台探测属性
        threading.Thread(target=self._probe_and_build, daemon=True).start()

    # ================================================================ UI
    def _build_ui_empty(self):
        """先显示一个空的探测中界面。"""
        root = QVBoxLayout(self)
        self.lbl_status = QLabel("正在探测摄像头能力...")
        self.lbl_status.setStyleSheet("color: #aaa; font-size: 11px;")
        root.addWidget(self.lbl_status)
        root.addStretch()

    def _probe_and_build(self):
        """后台探测属性，完成后重建 UI。"""
        self._props = _probe_camera_props(self._camera_index)
        # 切回主线程重建 UI
        QTimer.singleShot(0, self._build_ui_from_props)

    def _build_ui_from_props(self):
        """根据探测结果构建 UI。"""
        # 清空旧布局
        while self.layout().count():
            item = self.layout().takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        root = self.layout()
        root.setSpacing(10)

        supported_count = sum(
            1 for p in self._props.values() if p.get("supported")
        )
        if supported_count == 0:
            lbl = QLabel("该摄像头不支持手动参数调节")
            lbl.setStyleSheet("color: #aaa; font-size: 12px;")
            root.addWidget(lbl)
            root.addStretch()
            return

        # 状态信息
        self.lbl_status = QLabel(f"探测到 {supported_count} 个可调参数")
        self.lbl_status.setStyleSheet("color: #aaa; font-size: 11px;")
        root.addWidget(self.lbl_status)

        # 为每个支持的属性创建滑块
        self._sliders = {}

        prop_config = [
            ("brightness", "亮度", "0=最暗 | 右拉=最亮", int),
            ("contrast", "对比度", "0=最低 | 右拉=最高", int),
            ("saturation", "饱和度", "0=灰度 | 右拉=鲜艳", int),
            ("hue", "色调", "左拉=偏绿 | 右拉=偏紫", int),
            ("exposure", "曝光", "左拉=短曝光(暗) | 右拉=长曝光(亮)", int),
            ("focus", "对焦距离", "不支持此摄像头", int),
            ("gain", "增益(ISO)", "不支持此摄像头", int),
            ("zoom", "变焦", "不支持此摄像头", int),
        ]

        for prop_name, label, hint, cast_type in prop_config:
            info = self._props.get(prop_name, {})
            supported = info.get("supported", False)
            min_val = int(info.get("min", 0))
            max_val = int(info.get("max", 100))
            current = int(info.get("current", (min_val + max_val) // 2))

            grp = QGroupBox(label)
            grp_layout = QVBoxLayout(grp)

            row = QHBoxLayout()
            slider = QSlider(Qt.Horizontal)

            if supported:
                slider.setRange(min_val, max_val)
                slider.setValue(current)
                slider.setEnabled(True)
                slider.setStyleSheet("")
            else:
                slider.setRange(0, 100)
                slider.setValue(50)
                slider.setEnabled(False)
                slider.setStyleSheet("QSlider::groove:horizontal { background: #ddd; }")

            row.addWidget(slider, 1)

            if supported:
                lbl_val = QLabel(str(current))
            else:
                lbl_val = QLabel("不支持")
                lbl_val.setStyleSheet("color: #999;")
            lbl_val.setMinimumWidth(60)
            lbl_val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row.addWidget(lbl_val)

            grp_layout.addLayout(row)

            if supported:
                hint_lbl = QLabel(hint)
            else:
                hint_lbl = QLabel(f"该摄像头不支持{label}调节")
            hint_lbl.setStyleSheet("color: #888; font-size: 10px;")
            grp_layout.addWidget(hint_lbl)

            root.addWidget(grp)

            if supported:
                slider.valueChanged.connect(
                    lambda v, n=prop_name, l=lbl_val, c=cast_type: self._on_slider_changed(n, c(v), l)
                )
            self._sliders[prop_name] = slider

        root.addStretch()

    # ================================================================ 回调
    def _on_slider_changed(self, prop_name: str, value, lbl: QLabel):
        if self._updating:
            return
        lbl.setText(str(value))
        # 限流：200ms 内不重复发送
        now = time.time() * 1000
        if now - self._last_set_ms < 200:
            return
        self._last_set_ms = now
        prop_info = self._props.get(prop_name, {})
        prop_id = prop_info.get("prop")
        if prop_id is None:
            return
        threading.Thread(
            target=self._set_prop, args=(prop_id, value), daemon=True
        ).start()

    def _set_prop(self, prop_id: int, value):
        """后台线程设置摄像头属性。"""
        try:
            cap = cv2.VideoCapture(self._camera_index, cv2.CAP_DSHOW)
            if cap is not None and cap.isOpened():
                cap.set(prop_id, value)
                cap.release()
        except Exception:
            pass
