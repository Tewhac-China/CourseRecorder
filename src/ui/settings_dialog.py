"""设置对话框（只显示已下载的 ASR 模型）。"""

from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
)

from ..core.config import Config
from ..core.transcriber import discover_models
from ..core.translator import discover_translation_models


class SettingsDialog(QDialog):
    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("设置")
        self.setMinimumWidth(480)
        self._models = discover_models()
        self._setup_ui()
        self._load()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        form = QFormLayout()

        # --- ASR 模型（只显示已下载的） ---
        self.combo_model = QComboBox()
        for m in self._models:
            self.combo_model.addItem(m["label"], m)
        # 如果没有已下载的模型，显示提示
        if self.combo_model.count() == 0:
            self.combo_model.addItem("（未找到已下载的 ASR 模型）", None)
        form.addRow("ASR 模型", self.combo_model)

        # 安装提示
        self.lbl_install_hint = QLabel()
        self.lbl_install_hint.setWordWrap(True)
        self.lbl_install_hint.setObjectName("lbl_hint")
        form.addRow("", self.lbl_install_hint)
        self.combo_model.currentIndexChanged.connect(self._update_install_hint)
        self.combo_model.currentIndexChanged.connect(self._update_lang_options)

        self.combo_lang = QComboBox()
        self._whisper_langs = ["auto", "zh", "en", "ja", "ko", "fr", "de", "es", "ru"]
        self._sensevoice_langs = ["auto", "zh+en", "zh", "en", "ja", "ko", "yue"]
        self.combo_lang.addItems(self._whisper_langs)
        form.addRow("识别语言", self.combo_lang)

        # SenseVoice 语言过滤提示
        self.lbl_lang_hint = QLabel()
        self.lbl_lang_hint.setWordWrap(True)
        self.lbl_lang_hint.setObjectName("lbl_hint")
        form.addRow("", self.lbl_lang_hint)

        # --- 翻译设置 ---
        self._translation_models = discover_translation_models()

        self.combo_translate_enabled = QComboBox()
        self.combo_translate_enabled.addItem("禁用实时翻译", False)
        self.combo_translate_enabled.addItem("启用实时翻译", True)
        self.combo_translate_enabled.currentIndexChanged.connect(self._update_translate_visibility)
        form.addRow("实时翻译", self.combo_translate_enabled)

        self.combo_translate_model = QComboBox()
        for m in self._translation_models:
            self.combo_translate_model.addItem(m["label"], m["name"])
        form.addRow("翻译模型", self.combo_translate_model)

        self.combo_translate_target = QComboBox()
        self._translate_targets = [
            ("中文 (zh)", "zh"),
            ("English (en)", "en"),
            ("日本語 (ja)", "ja"),
            ("한국어 (ko)", "ko"),
        ]
        for label, _ in self._translate_targets:
            self.combo_translate_target.addItem(label)
        form.addRow("目标语言", self.combo_translate_target)

        self.combo_translate_quantize = QComboBox()
        self.combo_translate_quantize.addItem("INT4 (推荐, 省显存)", "int4")
        self.combo_translate_quantize.addItem("INT8 (更精确)", "int8")
        self.combo_translate_quantize.addItem("FP16 (最高质量)", "fp16")
        form.addRow("量化方式", self.combo_translate_quantize)

        self.combo_translate_gpu = QComboBox()
        self.combo_translate_gpu.addItem("GPU 加速 (推荐)", True)
        self.combo_translate_gpu.addItem("仅 CPU", False)
        form.addRow("翻译设备", self.combo_translate_gpu)

        # 翻译文本颜色
        self._translate_color = "#888888"
        color_row = QHBoxLayout()
        self.btn_translate_color = QPushButton()
        self.btn_translate_color.setFixedWidth(60)
        self.btn_translate_color.setFixedHeight(28)
        self.btn_translate_color.clicked.connect(self._pick_translate_color)
        self.lbl_translate_color = QLabel("#888888")
        color_row.addWidget(self.btn_translate_color)
        color_row.addWidget(self.lbl_translate_color)
        color_row.addStretch()
        form.addRow("翻译文本颜色", color_row)

        # 翻译安装提示
        self.lbl_translate_hint = QLabel()
        self.lbl_translate_hint.setWordWrap(True)
        self.lbl_translate_hint.setObjectName("lbl_hint")
        form.addRow("", self.lbl_translate_hint)

        # --- 幻灯片检测 ---
        self.slider_sens = QSlider(Qt.Horizontal)
        self.slider_sens.setRange(0, 100)
        self.lbl_sens = QLabel("60")
        h = QHBoxLayout()
        h.addWidget(self.slider_sens)
        h.addWidget(self.lbl_sens)
        self.slider_sens.valueChanged.connect(lambda v: self.lbl_sens.setText(str(v)))
        form.addRow("幻灯片检测灵敏度", h)

        self.spin_cooldown = QSpinBox()
        self.spin_cooldown.setRange(0, 30)
        self.spin_cooldown.setSuffix(" 秒")
        form.addRow("幻灯片冷却时间", self.spin_cooldown)

        # --- 输出目录 ---
        h2 = QHBoxLayout()
        self.edit_output = QLineEdit()
        self.edit_output.setReadOnly(True)
        self.btn_browse = QPushButton("浏览...")
        self.btn_browse.clicked.connect(self._browse_output)
        h2.addWidget(self.edit_output)
        h2.addWidget(self.btn_browse)
        form.addRow("输出目录", h2)

        # --- 摄像头分辨率模式 ---
        self.combo_resolution = QComboBox()
        self.combo_resolution.addItem("使用摄像头最高分辨率（原生，不缩放）", "max")
        self.combo_resolution.addItem("限制到1080P（降低性能开销）", "1080p")
        self.combo_resolution.setToolTip(
            "最高分辨率（推荐）：按摄像头原生像素输出，画面不做任何缩放\n"
            "限制到1080P：请求摄像头输出不超过 1920×1080，可节省带宽与性能\n"
            "（两种模式录制与预览都不会对画面做二次缩放）"
        )
        form.addRow("摄像头分辨率", self.combo_resolution)

        # --- 原始视频录制 ---
        self.combo_record_raw = QComboBox()
        self.combo_record_raw.addItem("录制原始视频", True)
        self.combo_record_raw.addItem("不录制原始视频", False)
        form.addRow("摄像头模式", self.combo_record_raw)

        layout.addLayout(form)

        self.button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.button_box.accepted.connect(self._save)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

        self._update_install_hint()
        self._update_lang_options()

    def _update_install_hint(self) -> None:
        data = self.combo_model.currentData()
        if data and not data.get("downloaded", True):
            backend = data.get("backend", "")
            label = data.get("label", "")
            if "依赖损坏" in label:
                self.lbl_install_hint.setText(
                    "sentencepiece 编译扩展损坏导致 segfault，请在本机运行：\n"
                    "pip install sentencepiece==0.1.99\n"
                    "然后删除 ~/.course_recorder/.funasr_ok 后重启程序"
                )
            elif backend == "sensevoice":
                self.lbl_install_hint.setText(
                    "安装命令（在本机运行）：\n"
                    "pip install funasr onnxruntime-gpu omegaconf antlr4-python3-runtime==4.9.3 torch_complex\n"
                    "首次使用时模型会自动从 ModelScope 下载（约 254MB）。"
                )
            else:
                self.lbl_install_hint.setText("该模型尚未下载。")
        else:
            self.lbl_install_hint.setText("")

    def _update_lang_options(self) -> None:
        """根据选中的 ASR 后端更新语言选项列表。"""
        data = self.combo_model.currentData()
        if data is None:
            return
        backend = data.get("backend", "whisper")
        current = self.combo_lang.currentText()

        self.combo_lang.blockSignals(True)
        self.combo_lang.clear()
        if backend == "sensevoice":
            self.combo_lang.addItems(self._sensevoice_langs)
            self.lbl_lang_hint.setText(
                "SenseVoice 语言过滤：auto=自动检测全部语言，zh+en=仅保留中英文片段（推荐）"
            )
        else:
            self.combo_lang.addItems(self._whisper_langs)
            self.lbl_lang_hint.setText("")

        # 尝试恢复之前的选择
        idx = self.combo_lang.findText(current)
        self.combo_lang.setCurrentIndex(max(0, idx))
        self.combo_lang.blockSignals(False)

    def _update_translate_visibility(self) -> None:
        """根据翻译开关显示/隐藏翻译相关控件。"""
        enabled = bool(self.combo_translate_enabled.currentData())
        self.combo_translate_model.setEnabled(enabled)
        self.combo_translate_target.setEnabled(enabled)
        self.combo_translate_quantize.setEnabled(enabled)
        self.combo_translate_gpu.setEnabled(enabled)
        self.btn_translate_color.setEnabled(enabled)
        if enabled:
            self.lbl_translate_hint.setText(
                "首次使用需从 HuggingFace 下载模型（约 3-4GB）。\n"
                "需要安装：pip install transformers accelerate bitsandbytes"
            )
        else:
            self.lbl_translate_hint.setText("")

    def _pick_translate_color(self) -> None:
        """弹出颜色选择器。"""
        color = QColorDialog.getColor(QColor(self._translate_color), self, "选择翻译文本颜色")
        if color.isValid():
            self._translate_color = color.name()
            self._update_color_btn()

    def _update_color_btn(self) -> None:
        """更新颜色按钮的显示。"""
        self.btn_translate_color.setStyleSheet(
            f"background-color: {self._translate_color}; border: 1px solid #555;"
        )
        self.lbl_translate_color.setText(self._translate_color)

    def _browse_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择输出目录", self.edit_output.text())
        if path:
            self.edit_output.setText(path)

    def _load(self) -> None:
        # 选中当前配置的模型（用原始值，不用 model_size 属性，后者会过滤非 Whisper 名称）
        current_backend = self.config.get("asr_backend", "whisper")
        current_model = str(self.config.get("asr_model_size", "base"))
        for i in range(self.combo_model.count()):
            data = self.combo_model.itemData(i)
            if data and data.get("backend") == current_backend and data.get("name") == current_model:
                self.combo_model.setCurrentIndex(i)
                break

        lang = self.config.language or "auto"
        self.combo_lang.setCurrentText(lang)
        self.slider_sens.setValue(self.config.sensitivity)
        self.lbl_sens.setText(str(self.config.sensitivity))
        self.spin_cooldown.setValue(int(self.config.get("slide_cooldown_sec", 2)))
        self.edit_output.setText(str(self.config.output_dir))
        # 摄像头分辨率模式
        resolution_mode = self.config.get("camera_resolution_mode", "max")
        for i in range(self.combo_resolution.count()):
            if self.combo_resolution.itemData(i) == resolution_mode:
                self.combo_resolution.setCurrentIndex(i)
                break
        record_raw = bool(self.config.get("record_raw_video", True))
        self.combo_record_raw.setCurrentIndex(0 if record_raw else 1)

        # 翻译设置
        translate_on = bool(self.config.get("translate_enabled", False))
        self.combo_translate_enabled.setCurrentIndex(1 if translate_on else 0)
        current_translate_model = self.config.get("translate_model", "Qwen/Qwen3-1.7B")
        for i in range(self.combo_translate_model.count()):
            if self.combo_translate_model.itemData(i) == current_translate_model:
                self.combo_translate_model.setCurrentIndex(i)
                break
        target_lang = self.config.get("translate_target_lang", "zh")
        for i, (_, code) in enumerate(self._translate_targets):
            if code == target_lang:
                self.combo_translate_target.setCurrentIndex(i)
                break
        quantize = self.config.get("translate_quantize", "int4")
        for i in range(self.combo_translate_quantize.count()):
            if self.combo_translate_quantize.itemData(i) == quantize:
                self.combo_translate_quantize.setCurrentIndex(i)
                break
        use_gpu = bool(self.config.get("translate_use_gpu", True))
        self.combo_translate_gpu.setCurrentIndex(0 if use_gpu else 1)
        self._translate_color = self.config.get("translate_color", "#888888")
        self._update_color_btn()
        self._update_translate_visibility()

    def _save(self) -> None:
        data = self.combo_model.currentData()
        if data:
            self.config.set("asr_backend", data.get("backend", "whisper"))
            self.config.set("asr_model_size", data.get("name", "base"))
        self.config.set("asr_language", self.combo_lang.currentText())
        self.config.set("slide_sensitivity", self.slider_sens.value())
        self.config.set("slide_cooldown_sec", self.spin_cooldown.value())
        self.config.set("output_dir", self.edit_output.text())
        # 摄像头分辨率模式
        resolution_mode = self.combo_resolution.currentData()
        if resolution_mode:
            self.config.set("camera_resolution_mode", resolution_mode)
        self.config.set("record_raw_video", bool(self.combo_record_raw.currentData()))

        # 翻译设置
        self.config.set("translate_enabled", bool(self.combo_translate_enabled.currentData()))
        translate_model = self.combo_translate_model.currentData()
        if translate_model:
            self.config.set("translate_model", translate_model)
        target_idx = self.combo_translate_target.currentIndex()
        if 0 <= target_idx < len(self._translate_targets):
            self.config.set("translate_target_lang", self._translate_targets[target_idx][1])
        quantize = self.combo_translate_quantize.currentData()
        if quantize:
            self.config.set("translate_quantize", quantize)
        self.config.set("translate_use_gpu", bool(self.combo_translate_gpu.currentData()))
        self.config.set("translate_color", self._translate_color)

        self.accept()
