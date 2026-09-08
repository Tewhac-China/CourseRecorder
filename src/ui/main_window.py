"""主窗口。"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2
import numpy as np
from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QMenu,
    QShortcut,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScroller,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSplitter,
    QTextEdit,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.audio_source import list_audio_devices
from ..core.config import Config
from ..core.media_source import list_cameras, create_video_source, check_camera_available, auto_fix_camera, force_release_camera
from ..core.nativecam_source import connect_nativecam, nativecam_info, _http_get, DEFAULT_PORT
from ..core.screen_detector import QuadStabilizer, ScreenDetector
from ..core.recorder import Recorder
from .preview_widget import PreviewWidget, ScaledImageLabel
from .settings_dialog import SettingsDialog
from .phone_camera_dialog import PhoneCameraDialog


class DeviceScanWorker(QThread):
    """后台扫描摄像头/音频设备。

    OpenCV 逐个探测摄像头索引很慢（每个失败的索引要 1 秒以上），
    放在主线程会让主窗口迟迟不出现，因此放到后台线程。
    摄像头扫描放到独立子进程中执行，DSHOW 崩溃不会带走整个程序。
    """

    finished_scan = pyqtSignal(list, list)

    def run(self) -> None:
        cams: List[dict] = []
        audios: List[dict] = []
        try:
            cams = self._scan_cameras_in_subprocess()
        except Exception as exc:
            print(f"[scan] camera error: {exc}")
            cams = []
        try:
            mics = list_audio_devices("input")
            for m in mics:
                m["type"] = "microphone"
            loops = [
                d
                for d in list_audio_devices("output")
                if d.get("is_loopback_capable")
            ]
            for lo in loops:
                lo["type"] = "loopback"
            audios = mics + loops
        except Exception as exc:
            print(f"[scan] audio error: {exc}")
            audios = []
        self.finished_scan.emit(cams, audios)

    @staticmethod
    def _scan_cameras_in_subprocess() -> List[dict]:
        """在子进程中扫描摄像头，DSHOW 崩溃不会带走主程序。

        子进程冷启动 + 导入 cv2 偶发较慢，单次可能超过超时返回空；
        因此失败（空结果且未崩溃）时最多重试一次。
        """
        import subprocess, json, sys
        script = (
            "import sys, json, os; os.environ['OPENCV_LOG_LEVEL']='SILENT'; "
            "sys.path.insert(0, r'%s'); "
            "from src.core.media_source import list_cameras; "
            "print(json.dumps(list_cameras()))"
        ) % str(Path(__file__).resolve().parent.parent.parent)
        last: List[dict] = []
        for attempt in range(2):
            try:
                result = subprocess.run(
                    [sys.executable, "-c", script],
                    capture_output=True, encoding="utf-8", errors="replace",
                    timeout=30,
                )
                if result.returncode == 0 and result.stdout.strip():
                    data = json.loads(result.stdout.strip().splitlines()[-1])
                    if data:
                        return data
                    last = data
            except Exception as e:
                print(f"[scan] subprocess error (attempt {attempt + 1}): {e}")
        return last


class PreviewWorker(QThread):
    """选了视频源后立即打开并逐帧显示，不录制。

    让用户在点"开始录制"之前就能看到画面，方便绘制角点。
    """

    # 注意: dewarped 可能为 None (角点无效/未标定), 因此用 object 而非 np.ndarray,
    # 否则 emit(None) 会抛 TypeError 导致预览线程直接退出 → 黑屏。
    frame_ready = pyqtSignal(object, object, float)  # overlay, dewarped(可为 None), ts
    camera_error = pyqtSignal(str)  # 摄像头错误通知

    _MAX_CONSECUTIVE_FAILS = 150  # 约 5 秒无帧 → 报错

    def __init__(self, source_cfg: dict, resolution_mode: str = "max",
                 target_aspect: float = 16.0 / 9.0, parent=None) -> None:
        super().__init__(parent)
        self._source_cfg = dict(source_cfg)
        self._resolution_mode = resolution_mode
        self._target_aspect = float(target_aspect)
        self._stop_event = threading.Event()
        self._manual_corners: Optional[np.ndarray] = None
        self._tracking = True
        self.last_corners: Optional[np.ndarray] = None  # 最近一帧使用的角点（原图坐标）
        self._last_frame: Optional[np.ndarray] = None   # 原始帧（供标定对话框自动检测）
        self._frame_lock = threading.Lock()
        self._source = None  # 供外部 stop() 时直接释放

    def set_corners(self, corners: Optional[np.ndarray]) -> None:
        self._manual_corners = corners

    def set_tracking(self, enabled: bool) -> None:
        """启用/禁用逐帧自动跟踪。禁用时由外部手动 set_corners。"""
        self._tracking = enabled
        if enabled:
            self._manual_corners = None

    def get_last_frame(self) -> Optional[np.ndarray]:
        """返回最近一帧原始画面（供标定对话框做自动角点检测）。"""
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
        emit_interval = 0.1  # 预览最多 10fps
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
                        self.camera_error.emit("摄像头持续无信号，请检查连接")
                        break
                    self.msleep(10)
                    continue

                consecutive_fails = 0

                # 保存原始帧（供标定对话框 get_last_frame 做自动检测）
                with self._frame_lock:
                    self._last_frame = frame.copy()

                if self._manual_corners is not None:
                    corners = self._manual_corners.copy()
                elif self._tracking:
                    corners = det.auto_detect(frame)
                    corners = stab.update(corners, frame.shape)
                else:
                    corners = None

                self.last_corners = (
                    None if corners is None else np.asarray(corners, dtype=np.float32).copy()
                )

                now = time.perf_counter()
                if now - last_emit >= emit_interval:
                    last_emit = now
                    overlay = det.draw_overlay(frame, corners) if corners is not None else frame
                    dewarped = None
                    if corners is not None and det.is_valid_corners(corners, frame.shape):
                        dewarped = det.dewarp(frame, corners)
                    # dewarped 可能为 None（角点无效/未标定），由上层决定回退原图
                    self.frame_ready.emit(overlay, dewarped, ts)
        except Exception as exc:
            print(f"[Preview] error: {exc}")
            self.camera_error.emit(f"预览异常: {exc}")
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


class MainWindow(QMainWindow):
    """CourseRecorder 主窗口。"""

    # 手机摄像头（NativeCam USB）连接结果信号（后台线程 → 主线程）
    _nativecam_result_signal = pyqtSignal(bool, str, object)
    # 手机相机重启完成信号（后台线程 → 主线程，成功后恢复预览）
    _phone_restart_done = pyqtSignal(bool, str)
    # 手机相机 3A 设置完成信号（后台线程 → 主线程，仅更新状态栏）
    _phone_setcam_done = pyqtSignal(bool, str)

    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self.recorder = Recorder(config, parent=self)
        self._preview_worker: Optional[PreviewWorker] = None
        self._slides: List[Dict[str, Any]] = []
        self._current_slide_index: int = -1
        manual = config.get("manual_corners")
        self._corners_confirmed = manual is not None and len(manual) == 4
        # 最近帧尺寸缓存（点选对焦区域时做逆透视映射用）
        self._last_frame_size: Optional[Tuple[int, int]] = None
        self._last_dewarped_size: Optional[Tuple[int, int]] = None
        self._setup_ui()
        self._connect_signals()
        self._refresh_sources()  # 扫描完成后会自动启动预览
        self._apply_config()
        # 预加载翻译模型（后台线程，不阻塞 UI）
        self._preload_translator()
        # 摄像头重试定时器：无摄像头时每5秒重新扫描
        self._camera_retry_timer = QTimer(self)
        self._camera_retry_timer.timeout.connect(self._retry_camera_scan)
        self._camera_retry_timer.start(5000)
        self.setWindowTitle("CourseRecorder - 课堂录制与实时转录")
        self.resize(1200, 700)
        # 用集中式状态机初始化按钮可用性（不再靠零散 setEnabled 维持）
        self._update_ui_state("idle")

    def _setup_ui(self) -> None:
        """按工作流组织界面：选设备 → (可选)标定 → 录制 → 回看。

        分组原则：
        - 顶部工具栏只放「设备选择」与「全局设置」（低频、一次性的准备操作）；
        - 录制控制独立成条并放在主视觉区，是最高频的主操作；
        - 每个操作按钮都放在它所作用的区域旁边（标定→预览区，
          提取/覆盖→幻灯片区），避免操作与对象分离。
        """
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(8, 8, 8, 8)

        # ══════════════ 顶部工具栏：设备选择 + 全局 ══════════════
        self.toolbar = QToolBar()
        self.toolbar.setMovable(False)
        self.toolbar.setStyleSheet("QToolBar { spacing: 3px; }")
        self.addToolBar(self.toolbar)

        _ctl_h = 26  # 统一控件高度

        self.combo_video = QComboBox()
        self.combo_video.setFixedHeight(_ctl_h)
        self.combo_video.setMinimumWidth(130)
        self.combo_video.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.combo_video.setStyleSheet("QComboBox { min-width: 130px; } QComboBox QAbstractItemView { min-width: 300px; }")
        self.combo_video.setToolTip("选择画面来源：摄像头 / 手机摄像头 / 视频文件")
        self.combo_audio = QComboBox()
        self.combo_audio.setFixedHeight(_ctl_h)
        self.combo_audio.setMinimumWidth(130)
        self.combo_audio.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.combo_audio.setStyleSheet("QComboBox { min-width: 130px; } QComboBox QAbstractItemView { min-width: 300px; }")
        self.combo_audio.setToolTip("选择声音来源：麦克风 / 系统声音 / 视频文件音轨")

        self.btn_refresh_camera = QPushButton("刷新设备")
        self.btn_refresh_camera.setFixedHeight(_ctl_h)
        self.btn_refresh_camera.setToolTip("重新扫描摄像头 / 音频设备（可发现新插入的设备）")
        # 摄像头操作：下拉式按钮区（主按钮 = 打开/停止预览，箭头 = 常用相机操作）
        self.btn_camera_toggle = QToolButton()
        self.btn_camera_toggle.setFixedHeight(_ctl_h)
        self.btn_camera_toggle.setObjectName("btn_camera_toggle")
        self._set_camera_toggle_state("off")  # 初始状态：蓝色"打开摄像头"
        self.btn_camera_toggle.setPopupMode(QToolButton.MenuButtonPopup)
        self.btn_camera_toggle.setToolTip(
            "点击：打开 / 停止实时预览（停止时释放摄像头）\n"
            "箭头：重启手机相机、设置手机分辨率等操作"
        )
        cam_menu = QMenu(self.btn_camera_toggle)
        self.act_cam_restart = cam_menu.addAction("重启手机相机")
        self.act_cam_restart.setToolTip(
            "手机摄像头画面卡住/黑屏时，远程重启手机端相机（无需在手机 App 上手动操作）"
        )
        self.act_cam_restart.triggered.connect(self._on_restart_phone_camera)
        cam_menu.addSeparator()
        self.act_cam_ae_auto = cam_menu.addAction("自动曝光（全画面测光）")
        self.act_cam_ae_auto.setToolTip("恢复自动曝光，按整个画面测光")
        self.act_cam_ae_auto.triggered.connect(self._on_phone_ae_auto)
        self.act_cam_pick_focus = cam_menu.addAction("点选对焦/测光区域")
        self.act_cam_pick_focus.setToolTip(
            "在画面上点一下，相机将以该位置为中心对焦并测光（区域约占画面 20%）"
        )
        self.act_cam_pick_focus.triggered.connect(self._on_pick_focus_region)
        self.act_cam_region_clear = cam_menu.addAction("恢复自动对焦/测光")
        self.act_cam_region_clear.setToolTip("清除点选的对焦/测光区域，恢复全画面自动")
        self.act_cam_region_clear.triggered.connect(self._on_phone_region_clear)
        cam_menu.addSeparator()
        self.act_phone_cam_config = cam_menu.addAction("手机摄像头配置...")
        self.act_phone_cam_config.setToolTip("打开滑块面板：对焦距离 / EV / ISO，与手机端 App 一致")
        self.act_phone_cam_config.triggered.connect(self._on_open_phone_camera_config)
        cam_menu.addSeparator()
        self.act_usb_cam_config = cam_menu.addAction("USB 摄像头设置...")
        self.act_usb_cam_config.setToolTip("打开滑块面板：亮度 / 对比度 / 饱和度 / 色调 / 曝光（自动探测可用范围）")
        self.act_usb_cam_config.triggered.connect(self._on_open_usb_camera_config)
        res_menu = cam_menu.addMenu("手机分辨率")
        for _label, _size in (("最高分辨率", "auto"), ("1920×1080", "1920x1080"),
                              ("1280×720", "1280x720")):
            _act = res_menu.addAction(_label)
            _act.triggered.connect(lambda _=False, s=_size: self._on_set_phone_resolution(s))
        cam_menu.setMinimumWidth(220)  # 菜单宽度不被按钮宽度局限
        self.btn_camera_toggle.setMenu(cam_menu)
        self.btn_settings = QPushButton("设置")
        self.btn_settings.setFixedHeight(_ctl_h)
        self.btn_open_dir = QPushButton("打开目录")
        self.btn_open_dir.setFixedHeight(_ctl_h)
        self.btn_open_dir.setToolTip("打开录制输出目录（笔记 / 幻灯片 / 音视频）")

        # 录制主操作与标定入口收进工具栏（原来在预览区上方独占两行，
        # 压缩了画面高度；收进工具栏后画面纵向空间最大化）
        self.btn_record = QPushButton("● 开始录制")
        self.btn_record.setFixedHeight(_ctl_h)
        self.btn_record.setObjectName("btn_start")
        self.btn_record.setToolTip("开始录制（自动检测投影区域；检测不准时可先手动标定）")

        self.btn_pause = QPushButton("⏸ 暂停")
        self.btn_pause.setFixedHeight(_ctl_h)
        self.btn_pause.setObjectName("btn_pause")
        self.btn_pause.setEnabled(False)  # 常显，仅录制中可用

        self.btn_calibrate = QPushButton("标定投影区域")
        self.btn_calibrate.setFixedHeight(_ctl_h)
        self.btn_calibrate.setToolTip(
            "手动框选屏幕四角。不标定也会自动检测（自动检测不准时再用这个功能）"
        )

        self.lbl_timer = QLabel("00:00:00")
        self.lbl_timer.setObjectName("lbl_timer")
        self.lbl_timer.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.lbl_hint = QLabel("")
        self.lbl_hint.setObjectName("lbl_hint")

        self.toolbar.addWidget(QLabel("视频源:"))
        self.toolbar.addWidget(self.combo_video)
        self.toolbar.addWidget(QLabel("音频源:"))
        self.toolbar.addWidget(self.combo_audio)
        self.toolbar.addSeparator()
        self.toolbar.addWidget(self.btn_record)
        self.toolbar.addWidget(self.btn_pause)
        self.toolbar.addWidget(self.btn_calibrate)
        self.toolbar.addSeparator()
        self.toolbar.addWidget(self.btn_refresh_camera)
        self.toolbar.addWidget(self.btn_camera_toggle)
        self.toolbar.addSeparator()
        self.toolbar.addWidget(self.btn_settings)
        self.toolbar.addWidget(self.btn_open_dir)
        # 弹簧把计时器推到工具栏最右
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.toolbar.addWidget(spacer)
        self.toolbar.addWidget(self.lbl_timer)

        # 状态栏：左侧临时消息 + 右侧常显阶段提示
        self.statusbar = self.statusBar()
        self.statusbar.addPermanentWidget(self.lbl_hint)
        self.statusbar.showMessage("就绪")

        # ══════════════ 主体：左=字幕，右=画面+幻灯片 ══════════════
        splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(splitter, 1)

        # 左侧：实时字幕（全高）
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setSpacing(6)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self.transcript_box = QTextEdit()
        self.transcript_box.setReadOnly(True)
        self.transcript_box.setPlaceholderText("实时字幕将显示在这里...")
        QScroller.grabGesture(self.transcript_box.viewport(), QScroller.TouchGesture)
        lbl_transcript = QLabel("实时字幕")
        lbl_transcript.setObjectName("lbl_section")
        left_layout.addWidget(lbl_transcript)
        left_layout.addWidget(self.transcript_box, 1)

        splitter.addWidget(left)

        # 右侧：上=投影画面，下=幻灯片
        # （录制/暂停/标定按钮已收进顶部工具栏，这里没有控制条与标题行，
        #   画面可以直接顶到工具栏下方，纵向屏占比最大化）
        right_splitter = QSplitter(Qt.Vertical)

        # 矫正画面：默认等比例填充控件区域显示原图全图
        # （1:1 原生像素模式会超出窗口被裁，故默认关闭）
        self.preview_dewarped = PreviewWidget()
        self.preview_dewarped.setMinimumSize(160, 120)
        self.preview_dewarped.allow_corner_selection(False)
        self.preview_dewarped.set_native_size(False)
        self.preview_scroll = QScrollArea()
        # widgetResizable=True → 内部 widget 填满视口，配合适配模式铺满
        self.preview_scroll.setWidgetResizable(True)
        self.preview_scroll.setWidget(self.preview_dewarped)
        self.preview_scroll.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.preview_scroll.setFrameShape(QScrollArea.NoFrame)
        right_splitter.addWidget(self.preview_scroll)

        # ── 右下：幻灯片（操作紧邻作用对象） ──
        slide_area = QWidget()
        slide_area_layout = QVBoxLayout(slide_area)
        slide_area_layout.setSpacing(4)
        slide_area_layout.setContentsMargins(0, 0, 0, 0)

        slide_nav = QWidget()
        slide_nav_layout = QHBoxLayout(slide_nav)
        slide_nav_layout.setContentsMargins(0, 0, 0, 0)
        slide_nav_layout.setSpacing(4)

        _btn_h = 26  # 统一按钮高度

        self.btn_prev_slide = QPushButton("<")
        self.btn_prev_slide.setFixedSize(28, _btn_h)
        self.btn_prev_slide.setEnabled(False)
        self.btn_prev_slide.setToolTip("上一张")
        self.slide_index_label = QLabel("幻灯片 0/0")
        self.slide_index_label.setAlignment(Qt.AlignCenter)
        self.slide_index_label.setObjectName("lbl_slide_index")
        self.btn_next_slide = QPushButton(">")
        self.btn_next_slide.setFixedSize(28, _btn_h)
        self.btn_next_slide.setEnabled(False)
        self.btn_next_slide.setToolTip("下一张")

        # 幻灯片操作：与导航同处一行，紧邻其作用的幻灯片
        self.btn_sensitivity = QPushButton("敏感度")
        self.btn_sensitivity.setFixedHeight(_btn_h)
        self.btn_sensitivity.setToolTip("拖动滑块调节幻灯片检测灵敏度（0=迟钝 100=敏感），立即生效")
        self.btn_capture = QPushButton("提取画面")
        self.btn_capture.setFixedHeight(_btn_h)
        self.btn_capture.setEnabled(False)
        self.btn_capture.setToolTip(
            "自动检测漏掉某一页时，点这里把当前画面立刻保存为一张幻灯片（Ctrl+E）"
        )
        self.btn_replace_slide = QPushButton("覆盖")
        self.btn_replace_slide.setFixedHeight(_btn_h)
        self.btn_replace_slide.setEnabled(False)
        self.btn_replace_slide.setToolTip("用当前画面覆盖选中的这张幻灯片")
        self.btn_undo_replace = QPushButton("撤回覆盖")
        self.btn_undo_replace.setFixedHeight(_btn_h)
        self.btn_undo_replace.setEnabled(False)
        self.btn_undo_replace.setToolTip("撤回上一次覆盖操作，恢复原幻灯片")
        self.btn_delete_slide = QPushButton("删除")
        self.btn_delete_slide.setFixedHeight(_btn_h)
        self.btn_delete_slide.setEnabled(False)
        self.btn_delete_slide.setToolTip("删除当前选中的幻灯片")

        slide_nav_layout.addWidget(self.btn_prev_slide)
        slide_nav_layout.addWidget(self.slide_index_label, 1)
        slide_nav_layout.addWidget(self.btn_next_slide)
        slide_nav_layout.addWidget(self.btn_sensitivity)
        slide_nav_layout.addWidget(self.btn_capture)
        slide_nav_layout.addWidget(self.btn_replace_slide)
        slide_nav_layout.addWidget(self.btn_undo_replace)
        slide_nav_layout.addWidget(self.btn_delete_slide)
        slide_area_layout.addWidget(slide_nav)

        self.slide_display = ScaledImageLabel()
        self.slide_display.setObjectName("slide_display")
        slide_area_layout.addWidget(self.slide_display, 1)

        right_splitter.addWidget(slide_area)
        # 实时画面与幻灯片记录图默认等高：
        # 幻灯片区多一行导航（约 30px），份额相应 +30/-30 补偿
        right_splitter.setSizes([530, 470])

        splitter.addWidget(right_splitter)
        splitter.setSizes([400, 800])

    def _connect_signals(self) -> None:
        self.btn_record.clicked.connect(self._on_record_toggle)
        self.btn_pause.clicked.connect(self._on_pause_toggle)
        self.btn_settings.clicked.connect(self._on_settings)
        self.btn_open_dir.clicked.connect(self._on_open_dir)
        self.btn_calibrate.clicked.connect(self._on_calibrate)
        self.btn_capture.clicked.connect(self._on_capture_slide)
        self.btn_replace_slide.clicked.connect(self._on_replace_slide)
        self.btn_undo_replace.clicked.connect(self._on_undo_replace_slide)
        self.btn_delete_slide.clicked.connect(self._on_delete_slide)
        self.btn_sensitivity.clicked.connect(self._on_open_sensitivity)
        self.btn_prev_slide.clicked.connect(self._on_prev_slide)
        self.btn_next_slide.clicked.connect(self._on_next_slide)
        self.btn_refresh_camera.clicked.connect(self._on_refresh_camera)
        self.btn_camera_toggle.clicked.connect(self._on_toggle_camera)
        self._phone_restart_done.connect(self._on_phone_restart_done)
        self._phone_setcam_done.connect(self._on_phone_setcam_done)
        self.preview_dewarped.point_picked.connect(self._on_point_picked)

        # Ctrl+E 手动提取当前幻灯片
        self.shortcut_capture = QShortcut(QKeySequence("Ctrl+E"), self)
        self.shortcut_capture.activated.connect(self._on_capture_slide)
        self.combo_video.currentIndexChanged.connect(self._on_video_changed)
        self.combo_audio.currentIndexChanged.connect(self._on_audio_changed)

        self._nativecam_result_signal.connect(self._on_nativecam_result, type=Qt.QueuedConnection)
        self.recorder.frame_updated.connect(self._on_frame, type=Qt.QueuedConnection)
        self.recorder.slide_captured.connect(self._on_slide, type=Qt.QueuedConnection)
        self.recorder.transcript_updated.connect(self._on_transcript, type=Qt.QueuedConnection)
        self.recorder.state_changed.connect(self._on_state_changed, type=Qt.QueuedConnection)
        self.recorder.error.connect(self._on_recorder_error, type=Qt.QueuedConnection)
        self.recorder.finished.connect(self._on_recorder_finished, type=Qt.QueuedConnection)

    def _refresh_sources(self) -> None:
        """填充下拉框。摄像头/音频设备改为后台扫描，避免启动时界面卡住。"""
        # 启动时先强制释放可能残留的摄像头（上次崩溃/强杀导致）
        force_release_camera()
        self.combo_video.blockSignals(True)
        self.combo_audio.blockSignals(True)
        self.combo_video.clear()
        self.combo_audio.clear()

        # 静态项先放进去，保证界面立刻可用
        self.combo_video.addItem("🚫 不启用画面记录", {"type": "none"})
        self.combo_video.addItem("📱 手机摄像头 (USB)", {"type": "nativecam"})
        self.combo_video.addItem("(扫描摄像头中...)", None)
        self.combo_video.addItem("选择视频文件...", {"type": "file_select"})
        last = self.config.get("last_video_file")
        if last and Path(last).exists():
            self.combo_video.addItem(f"最近: {Path(last).name}", {"type": "file", "path": last})

        self.combo_audio.addItem("(扫描音频设备中...)", None)
        self.combo_audio.addItem("视频文件音轨", {"type": "file"})

        self.combo_video.blockSignals(False)
        self.combo_audio.blockSignals(False)

        self._scan_worker = DeviceScanWorker(self)
        self._scan_worker.finished_scan.connect(self._on_devices_scanned)
        self._scan_worker.start()

    def _on_devices_scanned(self, cameras: List[dict], audios: List[dict]) -> None:
        """后台扫描完成后填充设备列表并恢复上次选择。"""
        self.combo_video.blockSignals(True)
        self.combo_audio.blockSignals(True)

        # 移除占位项和已有的摄像头项（防止重复扫描导致重复）
        for i in range(self.combo_video.count() - 1, -1, -1):
            data = self.combo_video.itemData(i)
            if data is None or (isinstance(data, dict) and data.get("type") == "camera"):
                self.combo_video.removeItem(i)
        for i in range(self.combo_audio.count() - 1, -1, -1):
            if self.combo_audio.itemData(i) is None:
                self.combo_audio.removeItem(i)

        insert_at = 0
        for cam in cameras:
            self.combo_video.insertItem(insert_at, cam["name"], {"type": "camera", **cam})
            insert_at += 1

        insert_at = 0
        for d in audios:
            prefix = "MIC " if d.get("max_input_channels", 0) > 0 else "LOOPBACK "
            self.combo_audio.insertItem(
                insert_at, f"{prefix}{d['name']} ({d['hostapi']})", {**d}
            )
            insert_at += 1

        self.combo_video.blockSignals(False)
        self.combo_audio.blockSignals(False)

        self._apply_device_selection()
        # 扫描完成，不自动启动预览 — 等用户手动点击"打开摄像头"

    def _apply_device_selection(self) -> None:
        """按配置选中上次使用的视频/音频设备；音频没有历史则自动选第一个麦克风。"""
        vsrc = self.config.video_source
        if vsrc.get("type") == "none":
            for i in range(self.combo_video.count()):
                data = self.combo_video.itemData(i)
                if data and data.get("type") == "none":
                    self.combo_video.setCurrentIndex(i)
                    break
        elif vsrc.get("type") == "nativecam":
            # 恢复手机摄像头选中状态（不自动发起连接，避免启动时打扰）
            self._select_nativecam_item()
        elif vsrc.get("type") == "file":
            path = vsrc.get("path")
            for i in range(self.combo_video.count()):
                data = self.combo_video.itemData(i)
                if data and data.get("type") == "file" and data.get("path") == path:
                    self.combo_video.setCurrentIndex(i)
                    break
        else:
            idx = vsrc.get("index", 0)
            for i in range(self.combo_video.count()):
                data = self.combo_video.itemData(i)
                if data and data.get("type") == "camera" and data.get("index") == idx:
                    self.combo_video.setCurrentIndex(i)
                    break

        # 音频：先按配置恢复，找不到则自动选第一个麦克风
        asrc = self.config.audio_source
        atype = asrc.get("type", "microphone")
        selected = False
        if atype == "file":
            for i in range(self.combo_audio.count()):
                data = self.combo_audio.itemData(i)
                if data and data.get("type") == "file":
                    self.combo_audio.setCurrentIndex(i)
                    selected = True
                    break
        if not selected:
            # 按 index + type 匹配
            aidx = asrc.get("index")
            for i in range(self.combo_audio.count()):
                data = self.combo_audio.itemData(i)
                if data and data.get("type") == atype:
                    if aidx is None or data.get("index") == aidx:
                        self.combo_audio.setCurrentIndex(i)
                        selected = True
                        break
        if not selected:
            # 兜底：选第一个麦克风
            for i in range(self.combo_audio.count()):
                data = self.combo_audio.itemData(i)
                if data and data.get("type") == "microphone":
                    self.combo_audio.setCurrentIndex(i)
                    selected = True
                    break

    def _apply_config(self) -> None:
        vsrc = self.config.video_source
        # 阻塞信号：本函数只是恢复上次选中的下拉项，不能在初始化/设置时
        # 误触发 currentIndexChanged → _on_video_changed → 提前打开摄像头，
        # 否则会和正在扫描摄像头的子进程抢同一台设备导致驱动崩溃/黑屏。
        self.combo_video.blockSignals(True)
        self.combo_audio.blockSignals(True)
        try:
            if vsrc.get("type") == "none":
                for i in range(self.combo_video.count()):
                    data = self.combo_video.itemData(i)
                    if data and data.get("type") == "none":
                        self.combo_video.setCurrentIndex(i)
                        break
            elif vsrc.get("type") == "nativecam":
                self._select_nativecam_item()
            elif vsrc.get("type") == "file":
                path = vsrc.get("path")
                for i in range(self.combo_video.count()):
                    data = self.combo_video.itemData(i)
                    if data and data.get("type") == "file" and data.get("path") == path:
                        self.combo_video.setCurrentIndex(i)
                        break
            else:
                idx = vsrc.get("index", 0)
                for i in range(self.combo_video.count()):
                    data = self.combo_video.itemData(i)
                    if data and data.get("type") == "camera" and data.get("index") == idx:
                        self.combo_video.setCurrentIndex(i)
                        break
        finally:
            self.combo_video.blockSignals(False)
            self.combo_audio.blockSignals(False)

        # 音频选择已在 _apply_device_selection 中处理
        # 角点：不在此处加载到 widget，等用户手动开启标定模式时再加载

    def _current_video_cfg(self) -> Optional[Dict[str, Any]]:
        data = self.combo_video.currentData()
        if data is None:
            return None
        if data.get("type") == "file_select":
            path, _ = QFileDialog.getOpenFileName(self, "选择视频文件", "", "视频文件 (*.mp4 *.avi *.mov *.mkv)")
            if path:
                self.config.set("last_video_file", path)
                # 添加并选中
                self.combo_video.blockSignals(True)
                self.combo_video.insertItem(0, f"文件: {Path(path).name}", {"type": "file", "path": path})
                self.combo_video.setCurrentIndex(0)
                self.combo_video.blockSignals(False)
                return {"type": "file", "path": path}
            return None
        return dict(data)

    def _current_audio_cfg(self) -> Dict[str, Any]:
        data = self.combo_audio.currentData()
        return dict(data) if data else {"type": "microphone"}

    def _on_video_changed(self) -> None:
        cfg = self._current_video_cfg()
        if cfg is None:
            return

        # 手机摄像头（NativeCam USB）连接流程
        if cfg.get("type") == "nativecam":
            self._connect_nativecam()
            return

        self.config.set("video_source", cfg)
        self.config.save()
        if self.recorder.state() == "idle":
            if cfg.get("type") == "none":
                # 不启用画面记录：停止预览，显示黑屏
                self._stop_preview()
                self.preview_dewarped.set_blackout()
                self._corners_confirmed = True
                self.statusbar.showMessage("已选择不启用画面记录 — 仅转录音频")
            # 切换视频源后同步角点状态（不自动启动预览，等用户手动点击"打开摄像头"）
            manual = self.config.get("manual_corners")
            self._corners_confirmed = bool(manual and len(manual) == 4)
            self._update_ui_state()

    def _on_audio_changed(self) -> None:
        cfg = self._current_audio_cfg()
        self.config.set("audio_source", cfg)
        self.config.save()

    # ------------------------------------------------------------ 手机摄像头（NativeCam USB）
    def _on_restart_phone_camera(self) -> None:
        """重启手机端相机（手机摄像头画面卡住/黑屏时的快捷修复）。"""
        cfg = self._current_video_cfg() or {}
        if cfg.get("type") != "nativecam":
            self.statusbar.showMessage("⚠ 当前视频源不是手机摄像头，无需重启手机相机")
            return
        self.statusbar.showMessage("正在重启手机端相机…（约 2~8 秒）")
        threading.Thread(target=self._do_restart_phone_camera, daemon=True).start()

    def _do_restart_phone_camera(self) -> None:
        port = int((self._current_video_cfg() or {}).get("port") or DEFAULT_PORT)
        ok = False
        msg = ""
        try:
            _http_get(f"http://127.0.0.1:{port}/restart", timeout=8)
            for _ in range(16):
                time.sleep(0.5)
                info = nativecam_info(port)
                if info and info.get("status") == "streaming":
                    ok = True
                    break
            msg = "手机端相机已重启" if ok else "重启后仍未恢复，请重新插拔 USB 或重连"
        except Exception as exc:
            msg = f"重启请求失败: {exc}"
        self._phone_restart_done.emit(ok, msg)

    def _on_phone_restart_done(self, ok: bool, msg: str) -> None:
        self.statusbar.showMessage(("✓ " if ok else "⚠ ") + msg)
        if ok and self.recorder.state() == "idle":
            self._start_preview()

    # ---- 相机 3A 快捷操作（通过手机端 /setcam） ----
    def _is_phone_source(self) -> bool:
        return (self._current_video_cfg() or {}).get("type") == "nativecam"

    def _phone_port(self) -> int:
        return int((self._current_video_cfg() or {}).get("port") or DEFAULT_PORT)

    def _phone_setcam_async(self, query: str, ok_msg: str) -> None:
        """后台发送 /setcam 参数，完成后更新状态栏。

        注意必须校验响应体: 手机端对不认识的参数会返回 {"result":"error",...},
        只看 HTTP 状态会误报成功（APK 版本过旧时的典型症状）。
        """

        def _run():
            try:
                resp = _http_get(
                    f"http://127.0.0.1:{self._phone_port()}/setcam?{query}", timeout=5
                ).decode("utf-8", "ignore")
                if '"ok"' in resp.replace(" ", ""):
                    self._phone_setcam_done.emit(True, ok_msg)
                else:
                    self._phone_setcam_done.emit(
                        False, f"手机端拒绝 ({resp.strip()[:120]}) — 请重新连接以升级手机端 App"
                    )
            except Exception as exc:
                self._phone_setcam_done.emit(False, f"设置失败: {exc}")

        threading.Thread(target=_run, daemon=True).start()

    def _on_phone_setcam_done(self, ok: bool, msg: str) -> None:
        self.statusbar.showMessage(("✓ " if ok else "⚠ ") + msg)

    def _on_phone_ae_auto(self) -> None:
        if not self._is_phone_source():
            self.statusbar.showMessage("⚠ 当前视频源不是手机摄像头")
            return
        self._phone_setcam_async(
            "ae=auto&ev=auto&iso=auto&region=clear", "已恢复全自动曝光（AE 重新接管）"
        )

    def _on_phone_region_clear(self) -> None:
        if not self._is_phone_source():
            self.statusbar.showMessage("⚠ 当前视频源不是手机摄像头")
            return
        self.preview_dewarped.set_point_pick_mode(False)
        self._phone_setcam_async("af=auto&region=clear", "已恢复自动对焦/全画面测光")

    def _on_open_phone_camera_config(self) -> None:
        """打开手机摄像头配置面板（EV/ISO/对焦距离滑块）。"""
        if not self._is_phone_source():
            self.statusbar.showMessage("⚠ 当前视频源不是手机摄像头")
            return
        port = self._phone_port()
        # 保存引用防止被 GC 回收
        if not hasattr(self, '_phone_cam_dlg') or self._phone_cam_dlg is None:
            self._phone_cam_dlg = PhoneCameraDialog(port=port, parent=self)
            self._phone_cam_dlg.point_pick_requested.connect(self._on_pick_focus_region)
        self._phone_cam_dlg.show()
        self._phone_cam_dlg.raise_()
        self._phone_cam_dlg.activateWindow()

    def _on_open_usb_camera_config(self) -> None:
        """打开 USB 摄像头配置面板（亮度/对比度/饱和度/色调/曝光滑块）。"""
        cfg = self._current_video_cfg() or {}
        if cfg.get("type") != "camera":
            self.statusbar.showMessage("⚠ 当前视频源不是 USB 摄像头")
            return
        camera_index = cfg.get("index", 0)
        if not hasattr(self, '_usb_cam_dlg') or self._usb_cam_dlg is None:
            from .usb_camera_dialog import UsbCameraDialog
            self._usb_cam_dlg = UsbCameraDialog(camera_index=camera_index, parent=self)
        self._usb_cam_dlg.show()
        self._usb_cam_dlg.raise_()
        self._usb_cam_dlg.activateWindow()

    def _on_pick_focus_region(self) -> None:
        """进入点选模式：用户在预览画面点一下即指定对焦/测光区域。"""
        if not self._is_phone_source():
            self.statusbar.showMessage("⚠ 当前视频源不是手机摄像头，无法点选对焦区域")
            return
        self.preview_dewarped.set_point_pick_mode(True)
        self.statusbar.showMessage("请在画面上点击要对焦/测光的位置（十字光标）")

    def _on_point_picked(self, x: float, y: float) -> None:
        """画面单点点击 → 手机端对焦/测光区域。

        若当前显示的是拉直图（dewarped），需要先把点击点经逆透视变换
        映射回相机原图坐标，再归一化发给手机。
        """
        self.preview_dewarped.set_point_pick_mode(False)
        fw, fh = self._last_frame_size or (0, 0)
        if fw <= 0 or fh <= 0:
            self.statusbar.showMessage("⚠ 暂无画面，无法设置对焦区域")
            return
        sx, sy = x, y
        corners = self._preview_worker.last_corners if self._preview_worker else None
        showing_dewarped = (
            self._corners_confirmed and corners is not None and self._last_dewarped_size
        )
        if showing_dewarped:
            dw, dh = self._last_dewarped_size
            dst = np.array([[0, 0], [dw - 1, 0], [dw - 1, dh - 1], [0, dh - 1]], np.float32)
            try:
                m = cv2.getPerspectiveTransform(dst, corners.astype(np.float32))
                pt = cv2.perspectiveTransform(
                    np.array([[[x, y]]], dtype=np.float32), m
                )[0][0]
                sx, sy = float(pt[0]), float(pt[1])
            except Exception:
                sx, sy = x, y
        nx = max(0.0, min(1.0, sx / fw))
        ny = max(0.0, min(1.0, sy / fh))
        self._phone_setcam_async(
            f"region={nx:.4f},{ny:.4f},0.20,0.20",
            f"对焦/测光区域已设为画面 ({nx:.0%}, {ny:.0%}) 处",
        )

    def _on_set_phone_resolution(self, size: str) -> None:
        """设置手机端相机分辨率（auto / 1920x1080 / 1280x720）。"""
        cfg = self._current_video_cfg() or {}
        if cfg.get("type") != "nativecam":
            self.statusbar.showMessage("⚠ 当前视频源不是手机摄像头，无法设置分辨率")
            return
        self.statusbar.showMessage(f"正在设置手机分辨率为 {size}…")
        threading.Thread(target=self._do_set_phone_resolution, args=(size,), daemon=True).start()

    def _do_set_phone_resolution(self, size: str) -> None:
        port = int((self._current_video_cfg() or {}).get("port") or DEFAULT_PORT)
        try:
            _http_get(f"http://127.0.0.1:{port}/setres?size={size}", timeout=8)
            time.sleep(1.5)
            info = nativecam_info(port)
            w = int((info or {}).get("width") or 0)
            h = int((info or {}).get("height") or 0)
            self._phone_restart_done.emit(True, f"手机分辨率已设为 {w}x{h}")
        except Exception as exc:
            self._phone_restart_done.emit(False, f"设置分辨率失败: {exc}")

    def _connect_nativecam(self) -> None:
        """手机摄像头 USB 连接流程（后台线程执行，避免阻塞 UI）。

        自动完成：adb forward → 安装 APK（若未装）→ 授权相机 →
        启动手机端服务 → 等待画面就绪。
        """
        self._stop_preview()
        self.preview_dewarped.set_blackout()
        self.statusbar.showMessage("正在连接手机摄像头 (USB)...")
        self.combo_video.setEnabled(False)
        self.btn_camera_toggle.setEnabled(False)

        def _do_connect():
            ok, cam_cfg, msg = connect_nativecam()
            self._nativecam_result_signal.emit(ok, msg, cam_cfg)

        threading.Thread(target=_do_connect, daemon=True).start()

    def _on_nativecam_result(self, ok: bool, msg: str, cam_cfg: Optional[dict]) -> None:
        """手机摄像头连接结果回调（主线程）。"""
        self.combo_video.setEnabled(True)
        self.btn_camera_toggle.setEnabled(True)

        if not ok or cam_cfg is None:
            self._set_camera_toggle_state("off")
            self.statusbar.showMessage(f"⚠ {msg}")
            QMessageBox.warning(self, "手机摄像头连接失败", msg)
            # 恢复到之前的选择
            self._apply_device_selection()
            return

        # 连接成功：在下拉框中添加/更新手机摄像头项并选中
        self.combo_video.blockSignals(True)
        label = cam_cfg["name"]
        found = False
        for i in range(self.combo_video.count()):
            data = self.combo_video.itemData(i)
            if data and data.get("type") == "nativecam" and data.get("is_connected"):
                self.combo_video.setItemText(i, label)
                self.combo_video.setItemData(i, {**cam_cfg, "is_connected": True})
                self.combo_video.setCurrentIndex(i)
                found = True
                break
        if not found:
            # 插到"手机摄像头"静态项之后（即摄像头列表最前面）
            self.combo_video.insertItem(0, label, {**cam_cfg, "is_connected": True})
            self.combo_video.setCurrentIndex(0)
        self.combo_video.blockSignals(False)

        # 保存配置
        self.config.set("video_source", cam_cfg)
        self.config.save()
        self.statusbar.showMessage(f"✓ {msg}")

        if self.recorder.state() == "idle":
            self._start_preview()
            manual = self.config.get("manual_corners")
            self._corners_confirmed = bool(manual and len(manual) == 4)
            self._update_ui_state()


    def _make_temp_preview_worker(self):
        """暂停录制时，为标定对话框临时创建一个预览 worker。

        暂停期间主预览已停止（_preview_worker 为 None，摄像头由录制器持有），
        但标定需要实时画面，因此临时起一个 worker；标定结束后由调用方
        stop() 并把摄像头归还录制器（acquire_camera）。
        """
        cfg = self._current_video_cfg()
        if cfg is None:
            return None
        resolution_mode = self.config.get("camera_resolution_mode", "max")
        w = PreviewWorker(
            cfg, resolution_mode,
            target_aspect=self.config.projection_aspect_ratio, parent=self,
        )
        manual = self.config.get("manual_corners")
        if manual and len(manual) == 4:
            w.set_corners(np.array(manual, dtype=np.float32))
        w.start()   # 不阻塞等首帧：标定对话框会自行等 frame_ready 信号显示画面
        return w

    def _on_calibrate(self) -> None:
        if self.recorder.state() in ("starting", "recording"):
            QMessageBox.warning(self, "提示", "录制中无法设置角点，请先暂停或停止录制")
            return

        is_paused = (self.recorder.state() == "paused")
        temp_worker = None

        if is_paused:
            # 暂停态：让出摄像头 → 等驱动释放 → 临时预览供标定取流
            self.recorder.release_camera()
            import time; time.sleep(0.3)
            temp_worker = self._make_temp_preview_worker()
            worker = temp_worker
        else:
            if self._preview_worker is None:
                QMessageBox.warning(self, "提示", "请先打开摄像头预览")
                return
            # 空闲态：复用主预览流（不停止/重启摄像头 → 秒开且不会闪退）
            worker = self._preview_worker

        if worker is None:
            # 预览没起来：暂停态要把摄像头赶紧还给录制器
            if is_paused:
                self.recorder.acquire_camera()
            QMessageBox.warning(self, "提示", "无法打开预览画面，请重试")
            return

        try:
            from .corner_calibration import CornerCalibrationDialog
            dlg = CornerCalibrationDialog(self.config, worker, parent=self)
            if dlg.exec_() == QDialog.Accepted:
                corners = dlg.get_confirmed_corners()
                if corners is not None:
                    self.recorder._manual_corners = corners
                    self.recorder._use_manual_corners = True
                else:
                    self.recorder._manual_corners = None
                    self.recorder._use_manual_corners = False
                self._corners_confirmed = True
                self.statusbar.showMessage("角点已确认")
            else:
                manual = self.config.get("manual_corners")
                self._corners_confirmed = bool(manual and len(manual) == 4)
        finally:
            if temp_worker is not None:
                temp_worker.stop()
                # 等驱动完全释放后再把设备交还录制器
                import time; time.sleep(0.3)
                self.recorder.acquire_camera()
            self._update_ui_state()

    def _select_nativecam_item(self) -> None:
        """静默选中手机摄像头下拉项（不触发 _on_video_changed 的自动连接）。"""
        self.combo_video.blockSignals(True)
        fallback_idx = -1
        for i in range(self.combo_video.count()):
            data = self.combo_video.itemData(i)
            if data and data.get("type") == "nativecam":
                if data.get("is_connected"):
                    self.combo_video.setCurrentIndex(i)
                    self.combo_video.blockSignals(False)
                    return
                if fallback_idx < 0:
                    fallback_idx = i
        if fallback_idx >= 0:
            self.combo_video.setCurrentIndex(fallback_idx)
        self.combo_video.blockSignals(False)

    def _set_camera_toggle_state(self, state: str) -> None:
        """同步"打开/关闭摄像头"按钮的文案和颜色。

        state: "off" | "connecting" | "on"
        """
        btn = self.btn_camera_toggle
        if state == "connecting":
            btn.setText("连接中...")
            btn.setStyleSheet(
                "QToolButton { background-color: #f0ad4e; color: #fff; "
                "border: 1px solid #eea236; border-radius: 3px; "
                "font-weight: bold; padding: 4px 12px; }"
            )
        elif state == "on":
            btn.setText("关闭摄像头")
            btn.setStyleSheet(
                "QToolButton { background-color: #5cb85c; color: #fff; "
                "border: 1px solid #4cae4c; border-radius: 3px; "
                "font-weight: bold; padding: 4px 12px; }"
            )
        else:  # off
            btn.setText("打开摄像头")
            btn.setStyleSheet(
                "QToolButton { background-color: #337ab7; color: #fff; "
                "border: 1px solid #2e6da4; border-radius: 3px; "
                "font-weight: bold; padding: 4px 12px; }"
            )

    def _start_preview(self) -> None:
        """启动预览：打开当前视频源并实时显示画面（不录制）。"""
        self._stop_preview()
        self.preview_dewarped.clear_blackout()
        # 确保摄像头设备完全释放后再重新打开
        import time; time.sleep(0.15)
        cfg = self._current_video_cfg()
        if cfg is None:
            return
        resolution_mode = self.config.get("camera_resolution_mode", "max")
        self._preview_worker = PreviewWorker(
            cfg, resolution_mode,
            target_aspect=self.config.projection_aspect_ratio, parent=self,
        )
        self._preview_worker.frame_ready.connect(self._on_preview_frame, type=Qt.QueuedConnection)
        self._preview_worker.camera_error.connect(self._on_camera_error, type=Qt.QueuedConnection)
        # 如果已有手动角点，传给预览线程
        manual = self.config.get("manual_corners")
        if manual and len(manual) == 4:
            self._preview_worker.set_corners(np.array(manual, dtype=np.float32))
        self._preview_worker.start()
        self._set_camera_toggle_state("connecting")
        self.preview_dewarped.set_connecting(True)
        self.statusbar.showMessage("正在连接摄像头...")

    def _stop_preview(self) -> None:
        """停止预览线程。"""
        if self._preview_worker is not None:
            try:
                self._preview_worker.frame_ready.disconnect(self._on_preview_frame)
            except TypeError:
                pass
            try:
                self._preview_worker.camera_error.disconnect(self._on_camera_error)
            except TypeError:
                pass
            self._preview_worker.stop()
            self._preview_worker = None
            self._set_camera_toggle_state("off")
            self.preview_dewarped.set_connecting(False)

    def _preload_translator(self) -> None:
        """应用启动时预加载翻译模型，避免录制时等待。"""
        if not self.config.get("translate_enabled", False):
            return
        from ..core.translator import Translator
        self._translator = Translator(
            model_name=self.config.get("translate_model", "Qwen/Qwen3-1.7B"),
            target_lang=self.config.get("translate_target_lang", "zh"),
            quantize=self.config.get("translate_quantize", "int4"),
            use_gpu=self.config.get("translate_use_gpu", True),
        )
        def _load():
            ok = self._translator.load()
            if ok:
                print("[MainWindow] 翻译模型预加载完成")
            else:
                print("[MainWindow] 翻译模型预加载失败")
        threading.Thread(target=_load, daemon=True).start()

    def _retry_camera_scan(self) -> None:
        """定时检查：无摄像头时自动重新扫描，发现后启动预览。"""
        if self.recorder.state() != "idle":
            return  # 录制中不扫描
        if self._preview_worker is not None:
            return  # 预览已在运行
        # 检查下拉框是否有摄像头选项（含手机摄像头项）
        has_camera = False
        for i in range(self.combo_video.count()):
            data = self.combo_video.itemData(i)
            if data and data.get("type") in ("camera", "nativecam"):
                has_camera = True
                break
        if has_camera:
            return  # 已有摄像头选项，不需要重扫（手动用"刷新设备"按钮）
        # 重新扫描
        self._refresh_sources()

    def _on_refresh_camera(self) -> None:
        """刷新检测摄像头。"""
        if self.recorder.state() in ("starting", "recording", "paused"):
            QMessageBox.warning(self, "提示", "录制中无法刷新摄像头，请先停止录制")
            return
        self._stop_preview()
        self._refresh_sources()
        self.statusbar.showMessage("正在重新扫描设备...")

    def _on_toggle_camera(self) -> None:
        """打开 / 关闭当前视频源的实时预览。"""
        if self.recorder.state() in ("starting", "recording", "paused"):
            QMessageBox.warning(self, "提示", "录制中无法切换摄像头，请先停止录制")
            return

        # 预览运行中 → 关闭
        if self._preview_worker is not None:
            self._stop_preview()
            self.preview_dewarped.set_blackout()
            self.statusbar.showMessage("摄像头已关闭")
            return

        # 预览未运行 → 打开
        cfg = self.combo_video.currentData()
        if cfg is None or cfg.get("type") == "file_select":
            QMessageBox.information(self, "提示", "请先选择视频源（摄像头 / 手机摄像头 / 视频文件）")
            return
        if cfg.get("type") == "none":
            QMessageBox.information(self, "提示", "当前为“不启用画面记录”模式，无需打开摄像头")
            return
        if cfg.get("type") == "nativecam" and not cfg.get("is_connected"):
            # 静态手机摄像头项：先走连接流程
            self._connect_nativecam()
            return
        self._start_preview()

    def _on_preview_frame(self, overlay: np.ndarray, dewarped: np.ndarray, ts: float) -> None:
        """预览线程的帧回调（非录制状态）。

        默认显示原图全图（等比例填充控件区域）；已标定投影区域后才显示拉直图像。
        """
        # 首帧到达 → 隐藏"连接中"覆盖层，切换按钮为"已连接"（绿色）
        if self._preview_worker is not None and self.preview_dewarped._connecting:
            self.preview_dewarped.set_connecting(False)
            self._set_camera_toggle_state("on")
            self.statusbar.showMessage("预览中")

        if self._corners_confirmed and dewarped is not None:
            self.preview_dewarped.set_image(dewarped)
            self._last_dewarped_size = (dewarped.shape[1], dewarped.shape[0])
        else:
            self.preview_dewarped.set_image(overlay)
        self._last_frame_size = (overlay.shape[1], overlay.shape[0])

    def _on_record_toggle(self) -> None:
        state = self.recorder.state()
        if state in ("starting", "recording", "paused"):
            # 当前在录制/暂停 → 停止
            self.recorder.stop()
        else:
            vcfg = self._current_video_cfg()
            if vcfg is None:
                QMessageBox.warning(self, "提示", "请选择视频源")
                return

            # 非"不启用画面记录"模式：录制前检测摄像头
            # （手机摄像头 USB 源不占用系统摄像头，无需检测）
            if vcfg.get("type") not in ("none", "nativecam"):
                ok, diag = check_camera_available()
                if not ok:
                    self.statusbar.showMessage(f"⚠ {diag} — 正在自动修复...")
                    fixed, fix_msg = auto_fix_camera()
                    if not fixed:
                        from PyQt5.QtWidgets import QMessageBox
                        QMessageBox.warning(self, "摄像头被占用", f"{diag}\n\n{fix_msg}")
                        return
                    self.statusbar.showMessage(f"✓ {fix_msg}")

            # 开始录制
            self._stop_preview()
            # 传递预加载的翻译器
            if hasattr(self, '_translator') and self._translator is not None:
                self.recorder.set_preloaded_translator(self._translator)
            self._slides.clear()
            self._current_slide_index = -1
            self._update_slide_display()
            acfg = self._current_audio_cfg()
            if vcfg.get("type") == "file" and acfg.get("type") == "file":
                acfg = {"type": "file"}
            self.statusbar.showMessage("启动中...")
            if not self.recorder.start(vcfg, acfg):
                pass  # 失败时 state_changed 会把按钮恢复

    def _on_pause_toggle(self) -> None:
        state = self.recorder.state()
        if state == "recording":
            self.recorder.pause()
        elif state == "paused":
            self.recorder.resume()

    def _on_capture_slide(self) -> None:
        """手动提取当前画面为幻灯片（自动检测漏拍时补拍）。"""
        if self.recorder.state() not in ("starting", "recording", "paused"):
            QMessageBox.information(
                self, "提示", '请先点击"开始录制"，再使用"提取当前画面"。'
            )
            return
        ok = self.recorder.capture_slide_now()
        if ok:
            self.statusbar.showMessage("已手动保存当前幻灯片")

    def _on_open_sensitivity(self) -> None:
        """弹出灵敏度滑块对话框，拖动即实时生效。"""
        cur = self.config.sensitivity
        dlg = SensitivityDialog(cur, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            val = dlg.value()
            self.config.set("slide_sensitivity", val)
            self.config.save()
            self.recorder.set_slide_sensitivity(val)
            self.statusbar.showMessage(f"幻灯片检测灵敏度已设为 {val}")

    def _on_replace_slide(self) -> None:
        """用当前画面覆盖已选中的幻灯片。"""
        if self.recorder.state() not in ("starting", "recording", "paused"):
            QMessageBox.information(self, "提示", '请先点击"开始录制"，再使用"覆盖当前页"。')
            return
        if not self._slides or self._current_slide_index < 0:
            QMessageBox.information(self, "提示", "请先用 < > 按钮选择要覆盖的幻灯片。")
            return
        info = self._slides[self._current_slide_index]
        path = info.get("path")
        if not path:
            QMessageBox.warning(self, "错误", "选中的幻灯片路径无效。")
            return
        ok = self.recorder.replace_slide(path)
        if ok:
            self._update_slide_display()
            self.statusbar.showMessage(f"已覆盖幻灯片 {self._current_slide_index + 1}")

    def _on_undo_replace_slide(self) -> None:
        """撤回覆盖：从备份恢复原幻灯片。"""
        if not self._slides or self._current_slide_index < 0:
            QMessageBox.information(self, "提示", "请先选择要撤回覆盖的幻灯片。")
            return
        info = self._slides[self._current_slide_index]
        path = info.get("path")
        if not path:
            return
        ok = self.recorder.undo_replace_slide(path)
        if ok:
            self._update_slide_display()
            self.statusbar.showMessage(f"已撤回覆盖幻灯片 {self._current_slide_index + 1}")

    def _on_delete_slide(self) -> None:
        """删除当前选中的幻灯片。"""
        if not self._slides or self._current_slide_index < 0:
            QMessageBox.information(self, "提示", "请先选择要删除的幻灯片。")
            return
        idx = self._current_slide_index
        info = self._slides[idx]
        path = info.get("path")
        ret = QMessageBox.question(
            self, "确认删除",
            f"确定要删除幻灯片 {idx + 1} 吗？此操作不可恢复。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        if path:
            self.recorder.delete_slide(path, idx)
        self._slides.pop(idx)
        if self._current_slide_index >= len(self._slides):
            self._current_slide_index = len(self._slides) - 1
        self._update_slide_display()
        self.statusbar.showMessage(f"已删除幻灯片 {idx + 1}")

    def _on_settings(self) -> None:
        dlg = SettingsDialog(self.config, self)
        if dlg.exec_():
            self.config.save()
            self._apply_config()

    def _on_open_dir(self) -> None:
        import os
        path = str(self.config.output_dir)
        os.makedirs(path, exist_ok=True)
        import subprocess
        subprocess.Popen(["explorer", path])

    def _on_frame(self, original: np.ndarray, dewarped: np.ndarray, ts: float) -> None:
        # 拉直图有效则显示拉直结果，否则兜底显示原始画面
        img = dewarped if dewarped is not None else original
        self.preview_dewarped.set_image(img)
        self._last_frame_size = (original.shape[1], original.shape[0])
        if dewarped is not None:
            self._last_dewarped_size = (dewarped.shape[1], dewarped.shape[0])
        self.statusbar.showMessage(f"录制中... {ts:.1f}s")
        # 录制计时（HH:MM:SS）
        t = int(ts)
        h, m, s = t // 3600, (t % 3600) // 60, t % 60
        self.lbl_timer.setText(f"{h:02d}:{m:02d}:{s:02d}")

    def _on_slide(self, info: Dict[str, Any]) -> None:
        self._slides.append(info)
        self._current_slide_index = len(self._slides) - 1
        self._update_slide_display()

    def _on_prev_slide(self) -> None:
        if self._current_slide_index > 0:
            self._current_slide_index -= 1
            self._update_slide_display()

    def _on_next_slide(self) -> None:
        if self._current_slide_index < len(self._slides) - 1:
            self._current_slide_index += 1
            self._update_slide_display()

    def _update_slide_display(self) -> None:
        from PyQt5.QtGui import QPixmap
        total = len(self._slides)
        if total == 0:
            self.slide_index_label.setText("幻灯片 0/0")
            self.slide_display.clear()
            self.btn_prev_slide.setEnabled(False)
            self.btn_next_slide.setEnabled(False)
            self._update_replace_btn()
            return
        idx = self._current_slide_index
        self.slide_index_label.setText(f"幻灯片 {idx + 1}/{total}")
        self.btn_prev_slide.setEnabled(idx > 0)
        self.btn_next_slide.setEnabled(idx < total - 1)
        info = self._slides[idx]
        path = info.get("path")
        if path and Path(path).exists():
            pix = QPixmap(path)
            if not pix.isNull():
                # 缩放交给 ScaledImageLabel.paintEvent：随控件尺寸等比例铺满
                self.slide_display.setPixmap(pix)
        self._update_replace_btn()

    def _update_replace_btn(self) -> None:
        """兼容旧调用：统一交给状态机处理。"""
        self._update_ui_state()

    def _on_transcript(self, segment: Dict[str, Any]) -> None:
        text = segment.get("text", "").strip()
        translated = segment.get("translated", "").strip()
        if text:
            start = segment.get("start", 0)
            self.transcript_box.append(f"[{start:.1f}s] {text}")
            if translated:
                color = self.config.get("translate_color", "#888888")
                self.transcript_box.append(
                    f'<span style="color:{color};">    📝 {translated}</span>'
                )

    def _apply_btn_style(self, btn: QPushButton) -> None:
        """重新套用样式：objectName 变更后需要 polish 才生效。"""
        btn.style().unpolish(btn)
        btn.style().polish(btn)

    def _update_ui_state(self, state: Optional[str] = None) -> None:
        """集中管理所有控件的可用性、文案与提示（唯一状态真相源）。

        工作流阶段: 选设备/预览 → (可选)标定 → 录制 → 停止保存。
        每个阶段只开放该阶段该做的事，避免用户面对一堆无意义的灰按钮。
        """
        state = state or self.recorder.state()
        idle = state == "idle"
        starting = state == "starting"
        recording = state in ("starting", "recording", "paused")
        paused = state == "paused"
        stopping = state == "stopping"

        # ── 录制主按钮（录制中是"停止"，其余是"开始"） ──
        self.btn_record.setText("■ 停止录制" if recording else "● 开始录制")
        new_name = "btn_stop" if recording else "btn_start"
        if self.btn_record.objectName() != new_name:
            self.btn_record.setObjectName(new_name)
        self._apply_btn_style(self.btn_record)
        # 说明：不再因为"未手动标定"就禁用录制 —— 未标定时会自动检测
        # 投影区域。只有"正在停止/保存"这一瞬态才禁用，避免重复点击。
        self.btn_record.setEnabled(not stopping)

        # ── 暂停 / 继续（常显，仅录制中可点，避免"按钮不见了"的困惑） ──
        self.btn_pause.setEnabled(recording and not stopping)
        self.btn_pause.setText("▶ 继续" if paused else "⏸ 暂停")

        # ── 设备类操作：只在空闲时可改（录制中换设备会中断录制） ──
        self.combo_video.setEnabled(idle)
        self.combo_audio.setEnabled(idle)
        self.btn_refresh_camera.setEnabled(idle)
        # 摄像头按钮：录制中仍可展开菜单（重启相机等操作），但切换预览需空闲
        self.btn_camera_toggle.setEnabled(True)

        # ── 标定：录制进行中不可标定，暂停/空闲可以 ──
        self.btn_calibrate.setEnabled(idle or paused)

        # ── 幻灯片操作：仅录制中可用（作用对象是当前画面） ──
        has_slide = 0 <= self._current_slide_index < len(self._slides)
        self.btn_capture.setEnabled(recording and not stopping)
        self.btn_replace_slide.setEnabled(
            recording and not stopping and has_slide
        )
        self.btn_undo_replace.setEnabled(has_slide)
        self.btn_delete_slide.setEnabled(has_slide)

        # ── 全局设置：录制中锁定，避免改配置影响当前录制 ──
        self.btn_settings.setEnabled(idle)
        self.btn_open_dir.setEnabled(True)

        self._update_hint(state)
        if not recording:
            self.lbl_timer.setText("00:00:00")

    def _update_hint(self, state: str) -> None:
        """更新控制条上的阶段提示，告诉用户「现在该做什么」。"""
        cfg = self._current_video_cfg() or {}
        vtype = cfg.get("type")
        if state == "starting":
            self.lbl_hint.setText("正在启动录制…")
        elif state == "recording":
            self.lbl_hint.setText("录制中 — 自动提取幻灯片并实时转写")
        elif state == "paused":
            self.lbl_hint.setText("已暂停 — 可点“继续”恢复，或结束录制")
        elif state == "stopping":
            self.lbl_hint.setText("正在保存录制结果…")
        elif state == "idle":
            if vtype is None:
                self.lbl_hint.setText("请选择视频源")
            elif vtype == "none":
                self.lbl_hint.setText("当前不记录画面，仅录制音频并转写")
            elif vtype == "nativecam" and not cfg.get("is_connected"):
                self.lbl_hint.setText("手机摄像头未连接 — 选中后会通过 USB 自动连接")
            elif self._corners_confirmed:
                self.lbl_hint.setText("已标定投影区域 — 可以开始录制")
            else:
                self.lbl_hint.setText(
                    "将自动检测投影区域（若检测不准，点“标定投影区域”手动框选）"
                )

    def _on_state_changed(self, state: str) -> None:
        self._update_ui_state(state)
        if state == "recording":
            self.statusbar.showMessage("录制中")
        elif state == "paused":
            self.statusbar.showMessage("已暂停")
        elif state == "idle":
            self.statusbar.showMessage("就绪")
            self._set_camera_toggle_state("off")
            # 空闲态自动恢复预览（"不启用画面记录"除外，它需要保持黑屏）
            cfg = self._current_video_cfg()
            if cfg and cfg.get("type") == "none":
                self.btn_calibrate.setEnabled(False)
            else:
                self._start_preview()
        elif state == "stopping":
            self.statusbar.showMessage("正在保存录制结果…")
        # 强制刷新样式
        self.btn_record.style().unpolish(self.btn_record)
        self.btn_record.style().polish(self.btn_record)

    def _on_camera_error(self, msg: str) -> None:
        """预览/标定线程报告摄像头错误 — 尝试自动修复。"""
        # 手机摄像头（USB）：不走系统摄像头修复流程，只提示
        cfg = self.combo_video.currentData()
        if isinstance(cfg, dict) and cfg.get("type") == "nativecam":
            self._set_camera_toggle_state("off")
            self.preview_dewarped.set_connecting(False)
            self.statusbar.showMessage(f"⚠ {msg} — 请检查手机 USB 连接后重试")
            return
        # 连接失败必须立即把按钮从"连接中"复位 —— 否则用户会一直看到黄色
        # "连接中"停在原地(此前正是这样: 失败后按钮永不复位, 看起来像卡死)。
        # 自动修复成功时 _start_preview() 会重新进入 connecting → on。
        self._set_camera_toggle_state("off")
        self.preview_dewarped.set_connecting(False)
        self.statusbar.showMessage(f"⚠ {msg} — 正在检测冲突...")
        # 在后台线程执行检测和修复，避免阻塞 UI
        threading.Thread(target=self._try_auto_fix_camera, daemon=True).start()

    def _try_auto_fix_camera(self) -> None:
        """后台检测摄像头冲突并尝试修复。"""
        import time
        ok, diag = check_camera_available()
        if ok:
            # 摄像头已恢复（可能是临时故障）
            self._start_preview()
            return

        # 尝试自动修复
        self.statusbar.showMessage(f"⚠ {diag} — 正在自动修复...")
        fixed, fix_msg = auto_fix_camera()

        if fixed:
            self.statusbar.showMessage(f"✓ {fix_msg}")
            time.sleep(1)
            self._start_preview()
        else:
            self.statusbar.showMessage(f"⚠ {fix_msg}")
            # 弹窗提示用户
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.warning(
                self, "摄像头被占用",
                f"{diag}\n\n{fix_msg}\n\n"
                "常见占用程序：微信、Teams、Zoom、OBS、浏览器等。\n"
                "请关闭占用程序后重试。",
            )

    def _on_recorder_error(self, msg: str) -> None:
        QMessageBox.critical(self, "录制错误", msg)

    def _on_recorder_finished(self) -> None:
        session_dir = self.recorder.current_session_dir()
        if session_dir and Path(session_dir).exists():
            self.statusbar.showMessage(f"录制已结束 — {session_dir}")
            QMessageBox.information(
                self, "完成",
                f"录制已结束，文件保存在：\n{session_dir}\n\n"
                f"包含：幻灯片图片、转写字幕、课堂笔记。",
            )
        else:
            self.statusbar.showMessage("录制已结束")

    def closeEvent(self, event) -> None:
        self._stop_preview()
        if self.recorder.state() in ("starting", "recording", "paused"):
            self.recorder.stop()
        # 等待录制清理完成（确保摄像头被释放）
        if hasattr(self.recorder, '_cleanup_thread') and self.recorder._cleanup_thread is not None:
            self.recorder._cleanup_thread.join(timeout=5)
        event.accept()


class SensitivityDialog(QDialog):
    """幻灯片检测灵敏度滑块对话框。"""

    def __init__(self, current: int = 60, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("调节幻灯片检测灵敏度")
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)

        hint = QLabel("灵敏度越高，越容易检测到幻灯片变化（0=迟钝，100=敏感）")
        hint.setStyleSheet("color: #888; font-size: 11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        row = QHBoxLayout()
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(current)
        row.addWidget(self.slider, 1)
        self.lbl = QLabel(str(current))
        self.lbl.setMinimumWidth(36)
        self.lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.lbl)
        layout.addLayout(row)

        self.slider.valueChanged.connect(lambda v: self.lbl.setText(str(v)))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def value(self) -> int:
        return self.slider.value()
