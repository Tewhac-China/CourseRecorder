"""手机摄像头配置对话框 — 与手机端 App 一致的滑块控制面板。

功能：
- EV 曝光补偿滑块
- ISO 感光度滑块（拖动即切手动曝光）
- 手动对焦距离滑块（屈光度，最左=自动）
- 点选对焦/测光区域
- 恢复全自动 / 重启相机
- 实时从 /info 同步当前状态
"""

from __future__ import annotations

import json
import threading
import time
from typing import Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import (
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..core.nativecam_source import _http_get, _http_json, DEFAULT_PORT


class PhoneCameraDialog(QDialog):
    """手机摄像头 3A 参数配置面板。"""

    # 点选对焦信号（转发给主窗口的预览控件）
    point_pick_requested = pyqtSignal()

    def __init__(self, port: int = DEFAULT_PORT, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self._updating_from_server = False  # 防止回写循环
        self._last_setcam_ms = 0.0
        self._focus_max = 20.0  # 最大屈光度（从设备读取后更新）
        self._ev_min, self._ev_max = -6, 6
        self._ev_step = 1.0 / 3.0  # 每档 EV 步长（从设备 /info evStep 读取后更新）
        self._iso_min, self._iso_max = 100, 6400

        self.setWindowTitle("手机摄像头配置")
        self.setMinimumWidth(420)
        self._build_ui()

        # 定时从 /info 同步状态
        self._sync_timer = QTimer(self)
        self._sync_timer.timeout.connect(self._sync_from_server)
        self._sync_timer.start(1500)
        self._sync_from_server()

    # ================================================================ UI
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        # ---- 状态信息 ----
        self.lbl_status = QLabel("正在连接...")
        self.lbl_status.setStyleSheet("color: #aaa; font-size: 11px;")
        root.addWidget(self.lbl_status)

        # ---- 对焦距离 ----
        grp_focus = QGroupBox("手动对焦距离")
        lay_focus = QVBoxLayout(grp_focus)

        row_focus = QHBoxLayout()
        self.slider_focus = QSlider(Qt.Horizontal)
        self.slider_focus.setRange(0, 200)  # 0=自动, 1..200 => 0.5~20 屈光度
        self.slider_focus.setValue(0)
        row_focus.addWidget(self.slider_focus, 1)
        self.lbl_focus = QLabel("自动")
        self.lbl_focus.setMinimumWidth(110)
        self.lbl_focus.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row_focus.addWidget(self.lbl_focus)
        lay_focus.addLayout(row_focus)

        hint_focus = QLabel("最左=自动对焦 | 右拉=手动近距离（屈光度=1/米）")
        hint_focus.setStyleSheet("color: #888; font-size: 10px;")
        lay_focus.addWidget(hint_focus)
        root.addWidget(grp_focus)

        # ---- EV 曝光补偿 ----
        grp_ev = QGroupBox("EV 曝光补偿")
        lay_ev = QVBoxLayout(grp_ev)

        row_ev = QHBoxLayout()
        self.slider_ev = QSlider(Qt.Horizontal)
        self.slider_ev.setRange(-6, 6)
        self.slider_ev.setValue(0)
        row_ev.addWidget(self.slider_ev, 1)
        self.lbl_ev = QLabel("0")
        self.lbl_ev.setMinimumWidth(40)
        self.lbl_ev.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row_ev.addWidget(self.lbl_ev)
        lay_ev.addLayout(row_ev)

        hint_ev = QLabel("0=自动基准 | 左拉=欠曝 | 右拉=过曝（EV 需自动曝光 AE 才生效）")
        hint_ev.setStyleSheet("color: #888; font-size: 10px;")
        lay_ev.addWidget(hint_ev)
        root.addWidget(grp_ev)

        # ---- ISO 感光度 ----
        grp_iso = QGroupBox("ISO 感光度（拖动即切手动曝光）")
        lay_iso = QVBoxLayout(grp_iso)

        row_iso = QHBoxLayout()
        self.slider_iso = QSlider(Qt.Horizontal)
        self.slider_iso.setRange(0, 100)  # 对数映射到 isoMin..isoMax
        self.slider_iso.setValue(0)
        row_iso.addWidget(self.slider_iso, 1)
        self.lbl_iso = QLabel("auto")
        self.lbl_iso.setMinimumWidth(60)
        self.lbl_iso.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row_iso.addWidget(self.lbl_iso)
        lay_iso.addLayout(row_iso)

        hint_iso = QLabel("最左=自动 ISO | 右拉=手动 ISO（自动切 AE OFF）")
        hint_iso.setStyleSheet("color: #888; font-size: 10px;")
        lay_iso.addWidget(hint_iso)
        root.addWidget(grp_iso)

        # ---- 快捷按钮 ----
        btn_row = QHBoxLayout()

        btn_pick = QPushButton("点选对焦/测光区域")
        btn_pick.setToolTip("在预览画面上点一下指定对焦/测光位置")
        btn_pick.clicked.connect(self._on_pick_focus)
        btn_row.addWidget(btn_pick)

        btn_reset = QPushButton("恢复全自动")
        btn_reset.setToolTip("恢复自动曝光、自动对焦、自动白平衡")
        btn_reset.clicked.connect(self._on_reset_all)
        btn_row.addWidget(btn_reset)

        btn_restart = QPushButton("重启相机")
        btn_restart.clicked.connect(self._on_restart)
        btn_row.addWidget(btn_restart)

        root.addLayout(btn_row)

        # ---- 信号连接 ----
        self.slider_focus.valueChanged.connect(self._on_focus_changed)
        self.slider_ev.valueChanged.connect(self._on_ev_changed)
        self.slider_iso.valueChanged.connect(self._on_iso_changed)

    # ================================================================ 滑块回调
    def _on_focus_changed(self, progress: int):
        if self._updating_from_server:
            return
        if progress == 0:
            self.lbl_focus.setText("自动")
            self._send_setcam("af=auto")
        else:
            diopter = self._progress_to_diopter(progress)
            dist_cm = 100.0 / diopter
            self.lbl_focus.setText(f"{diopter:.1f}D ({dist_cm:.0f}cm)")
            self._send_setcam(f"focus={diopter}")

    def _on_ev_changed(self, value: int):
        if self._updating_from_server:
            return
        self.lbl_ev.setText(self._ev_text(value))
        self._send_setcam(f"ev={value}")

    def _ev_text(self, idx: int) -> str:
        """把 EV 索引换算为用户易懂的 EV 值（索引 × 步长）。

        Camera2 的 CONTROL_AE_EXPOSURE_COMPENSATION 用的是"档位索引"
        （如 -20..20），真实 EV = 索引 × CONTROL_AE_COMPENSATION_STEP
        （常见 1/3 EV）。直接显示索引会让用户误以为是 ±20 EV。
        """
        try:
            return f"{idx * float(self._ev_step):+.1f}EV"
        except Exception:
            return str(idx)

    def _on_iso_changed(self, progress: int):
        if self._updating_from_server:
            return
        if progress == 0:
            self.lbl_iso.setText("auto")
            self._send_setcam("iso=auto")
        else:
            iso = self._progress_to_iso(progress)
            self.lbl_iso.setText(str(iso))
            self._send_setcam(f"iso={iso}")

    # ================================================================ 按钮
    def _on_pick_focus(self):
        """请求主窗口进入点选模式。"""
        self.point_pick_requested.emit()
        self.statusTip = "请在预览画面上点击对焦位置"
        self.lbl_focus.setText("请点击画面...")

    def _on_reset_all(self):
        self._send_setcam("reset=1")
        self._updating_from_server = True
        self.slider_focus.setValue(0)
        self.slider_ev.setValue(0)
        self.slider_iso.setValue(0)
        self.lbl_focus.setText("自动")
        self.lbl_ev.setText("0")
        self.lbl_iso.setText("auto")
        self._updating_from_server = False

    def _on_restart(self):
        threading.Thread(
            target=lambda: self._http_get_safe("/restart"), daemon=True
        ).start()

    # ================================================================ 状态同步
    def _sync_from_server(self):
        """从 /info 读取当前 3A 状态并同步滑块（不触发回写）。"""
        try:
            info = _http_json(f"{self.base}/info", timeout=3)
        except Exception:
            info = None

        if info is None or info.get("status") != "streaming":
            self.lbl_status.setText("服务未响应")
            return

        w = info.get("width", 0)
        h = info.get("height", 0)
        fps = info.get("fps", 0)
        cam = info.get("cam", {})
        af = cam.get("af", "?")
        fd = cam.get("focusDist", -1)
        ae = cam.get("ae", "?")
        ev = cam.get("ev", 0)
        iso_val = cam.get("iso", -1)

        # 更新设备实际能力范围
        fmax = cam.get("focusMax", 0)
        if fmax > 0 and abs(fmax - self._focus_max) > 0.1:
            self._focus_max = float(fmax)
        ev_min = cam.get("evMin", -6)
        ev_max = cam.get("evMax", 6)
        ev_step = cam.get("evStep", 1.0 / 3.0)
        iso_min = cam.get("isoMin", 100)
        iso_max = cam.get("isoMax", 6400)
        if ev_min != self._ev_min or ev_max != self._ev_max:
            self._ev_min, self._ev_max = ev_min, ev_max
            if not self.slider_ev.isSliderDown():
                self.slider_ev.setRange(ev_min, ev_max)
        try:
            st = float(ev_step)
            if st > 0:
                self._ev_step = st
        except Exception:
            pass
        if iso_min > 0 and iso_max > iso_min:
            if iso_min != self._iso_min or iso_max != self._iso_max:
                self._iso_min, self._iso_max = iso_min, iso_max

        focus_info = f"{fd:.1f}D" if fd > 0 else af
        self.lbl_status.setText(
            f"{w}×{h}  {fps:.1f}fps | AF={focus_info}  AE={ae}  EV={ev}"
        )

        # 同步滑块（防抖：用户正在拖动时不覆盖）
        self._updating_from_server = True
        try:
            # 对焦
            if not self.slider_focus.isSliderDown():
                if fd > 0:
                    prog = self._diopter_to_progress(fd)
                else:
                    prog = 0
                self.slider_focus.setValue(prog)
                if prog == 0:
                    self.lbl_focus.setText("自动")
                else:
                    d = self._progress_to_diopter(prog)
                    self.lbl_focus.setText(f"{d:.1f}D ({100/d:.0f}cm)")

            # EV
            if not self.slider_ev.isSliderDown():
                self.slider_ev.setValue(ev)
                self.lbl_ev.setText(self._ev_text(ev))

            # ISO
            if not self.slider_iso.isSliderDown():
                if iso_val > 0:
                    prog = self._iso_to_progress(iso_val)
                    self.slider_iso.setValue(prog)
                    self.lbl_iso.setText(str(iso_val))
                else:
                    self.slider_iso.setValue(0)
                    self.lbl_iso.setText("auto")
        finally:
            self._updating_from_server = False

    # ================================================================ 工具
    def _send_setcam(self, query: str):
        """后台发送 /setcam 请求（限流 200ms）。"""
        now = time.time() * 1000
        if now - self._last_setcam_ms < 200:
            return
        self._last_setcam_ms = now
        threading.Thread(
            target=self._http_get_safe,
            args=(f"/setcam?{query}",),
            daemon=True,
        ).start()

    def _http_get_safe(self, path: str):
        try:
            _http_get(f"{self.base}{path}", timeout=5)
        except Exception:
            pass

    def _progress_to_diopter(self, p: int) -> float:
        """progress 1..200 -> 屈光度 (指数映射, 范围由设备实际能力决定)。"""
        fmax = max(1.0, self._focus_max)
        return 0.5 * ((fmax / 0.5) ** (p / 200.0))

    def _diopter_to_progress(self, d: float) -> int:
        """屈光度 -> progress (反函数)。"""
        fmax = max(1.0, self._focus_max)
        if d <= 0.5:
            return 1
        if d >= fmax:
            return 200
        import math
        return max(1, min(200, round(200.0 * math.log(d / 0.5) / math.log(fmax / 0.5))))

    def _progress_to_iso(self, p: int) -> int:
        """progress 1..100 -> ISO (对数映射, 范围由设备实际能力决定)。"""
        lo = max(50, self._iso_min)
        hi = max(lo + 1, self._iso_max)
        iso = round(lo * ((hi / lo) ** (p / 100.0)))
        return max(lo, round(iso / 50) * 50)

    def _iso_to_progress(self, iso: int) -> int:
        """ISO -> progress (反函数)。"""
        lo = max(50, self._iso_min)
        hi = max(lo + 1, self._iso_max)
        if iso <= lo:
            return 1
        if iso >= hi:
            return 100
        import math
        return max(1, min(100, round(100.0 * math.log(iso / lo) / math.log(hi / lo))))
