"""全局主题样式。

深色未来感设计，纯 QSS 实现，零额外 GPU 开销。
所有 UI 模块通过 ``import theme`` 获取统一配色。
"""

# ── 调色板 ──────────────────────────────────────────────
# 背景层级：BG0 最深 → BG3 最浅
BG0 = "#0b0d12"       # 窗口底色
BG1 = "#12151c"       # 面板/工具栏
BG2 = "#1a1e28"       # 卡片/输入框
BG3 = "#242836"       # 悬浮态/选中行

BORDER  = "#2a2f3d"   # 边框
BORDER2 = "#363c50"   # 强调边框

TEXT   = "#e2e6f0"     # 主文字
TEXT2  = "#8890a4"     # 次要文字
TEXT3  = "#5c6378"     # 占位符/禁用

ACCENT      = "#00d4aa"  # 主强调色（青绿）
ACCENT_HOVER= "#00f0c6"
ACCENT_DIM  = "#00d4aa40" # 半透明强调（用于选中态背景）

DANGER  = "#f04050"
SUCCESS = "#34d399"
WARNING = "#fbbf24"

# ── 通用控件样式 ────────────────────────────────────────
GLOBAL_QSS = f"""
/* ── 全局 ── */
* {{
    font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif;
    font-size: 13px;
    color: {TEXT};
}}

QMainWindow, QDialog {{
    background-color: {BG0};
}}

QWidget {{
    background-color: transparent;
}}

/* ── 工具栏 ── */
QToolBar {{
    background-color: {BG1};
    border-bottom: 1px solid {BORDER};
    spacing: 6px;
    padding: 4px 8px;
}}
QToolBar::separator {{
    width: 1px;
    background: {BORDER};
    margin: 4px 4px;
}}

/* ── 按钮 ── */
QPushButton {{
    background-color: {BG2};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 5px 14px;
    min-height: 22px;
}}
/* ── 工具栏按钮：更紧凑，避免按钮过多时工具栏过宽/过高 ── */
QToolBar QPushButton {{
    padding: 3px 9px;
    font-size: 12px;
    min-height: 18px;
}}
/* ── 摄像头操作：下拉式按钮区（QToolButton + 菜单） ── */
QToolButton#btn_camera_toggle {{
    background-color: {BG2};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 3px 9px;
    font-size: 12px;
    min-height: 18px;
}}
QToolButton#btn_camera_toggle:hover {{
    background-color: {BG3};
    border-color: {BORDER2};
}}
QToolButton#btn_camera_toggle::menu-button {{
    border: none;
    width: 14px;
    background: transparent;
}}
QToolButton#btn_camera_toggle::menu-indicator {{
    subcontrol-position: right center;
}}
QPushButton:hover {{
    background-color: {BG3};
    border-color: {BORDER2};
}}
QPushButton:pressed {{
    background-color: {ACCENT_DIM};
    border-color: {ACCENT};
}}
QPushButton:disabled {{
    color: {TEXT3};
    background-color: {BG1};
    border-color: {BORDER};
}}
QPushButton:checked {{
    background-color: {ACCENT_DIM};
    border-color: {ACCENT};
    color: {ACCENT};
}}

/* ── 主操作按钮（开始录制） ── */
QPushButton#btn_start, QPushButton#btn_confirm {{
    background-color: {ACCENT};
    color: {BG0};
    font-weight: bold;
    border: none;
}}
QPushButton#btn_start:hover, QPushButton#btn_confirm:hover {{
    background-color: {ACCENT_HOVER};
}}
QPushButton#btn_start:disabled, QPushButton#btn_confirm:disabled {{
    background-color: {BG2};
    color: {TEXT3};
}}

/* ── 停止按钮 ── */
QPushButton#btn_stop {{
    background-color: {DANGER};
    color: #fff;
    border: none;
}}
QPushButton#btn_stop:hover {{
    background-color: #ff5a6a;
}}
QPushButton#btn_stop:disabled {{
    background-color: {BG2};
    color: {TEXT3};
}}

/* ── 暂停按钮（录制控制条上的 QPushButton） ── */
QPushButton#btn_pause, QToolButton#btn_pause {{
    background-color: {WARNING};
    color: {BG0};
    border: none;
    border-radius: 4px;
    padding: 5px 14px;
    font-weight: bold;
}}
QPushButton#btn_pause:hover, QToolButton#btn_pause:hover {{
    background-color: #fcd34d;
}}
QPushButton#btn_pause:disabled, QToolButton#btn_pause:disabled {{
    background-color: {BG2};
    color: {TEXT3};
}}

/* ── 下拉框 ── */
QComboBox {{
    background-color: {BG2};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 10px;
    min-height: 22px;
}}
QComboBox:hover {{
    border-color: {BORDER2};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox::down-arrow {{
    image: none;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid {TEXT2};
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background-color: {BG2};
    color: {TEXT};
    border: 1px solid {BORDER2};
    selection-background-color: {ACCENT_DIM};
    selection-color: {ACCENT};
    outline: none;
}}

/* ── 文本编辑（字幕区） ── */
QTextEdit, QPlainTextEdit {{
    background-color: {BG1};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 8px;
    selection-background-color: {ACCENT_DIM};
    selection-color: {ACCENT};
}}

/* ── 输入框 ── */
QLineEdit {{
    background-color: {BG2};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 8px;
    min-height: 22px;
}}
QLineEdit:focus {{
    border-color: {ACCENT};
}}
QLineEdit:read-only {{
    background-color: {BG1};
    color: {TEXT2};
}}

/* ── 标签 ── */
QLabel {{
    color: {TEXT};
    background: transparent;
}}
QLabel#lbl_hint {{
    color: {TEXT2};
    font-size: 11px;
}}
QLabel#lbl_section {{
    color: {TEXT2};
    font-size: 11px;
    font-weight: bold;
    letter-spacing: 1px;
    text-transform: uppercase;
}}

/* ── 滑块 ── */
QSlider::groove:horizontal {{
    height: 4px;
    background: {BORDER};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {ACCENT};
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 2px;
}}

/* ── 数字输入框 ── */
QSpinBox {{
    background-color: {BG2};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 8px;
    min-height: 22px;
}}
QSpinBox:focus {{
    border-color: {ACCENT};
}}
QSpinBox::up-button, QSpinBox::down-button {{
    background-color: {BG3};
    border: none;
    width: 18px;
}}
QSpinBox::up-arrow {{
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid {TEXT2};
}}
QSpinBox::down-arrow {{
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {TEXT2};
}}

/* ── 分割器 ── */
QSplitter::handle {{
    background: {BORDER};
}}
QSplitter::handle:horizontal {{
    width: 2px;
}}
QSplitter::handle:vertical {{
    height: 2px;
}}

/* ── 状态栏 ── */
QStatusBar {{
    background-color: {BG1};
    color: {TEXT2};
    border-top: 1px solid {BORDER};
    font-size: 12px;
    padding: 2px 8px;
}}

/* ── 滚动条 ── */
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER2};
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {TEXT3};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
    height: 0;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 8px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER2};
    border-radius: 4px;
    min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {TEXT3};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent;
    width: 0;
}}

/* ── 消息框 ── */
QMessageBox {{
    background-color: {BG1};
}}
QMessageBox QLabel {{
    color: {TEXT};
}}
QMessageBox QPushButton {{
    min-width: 80px;
}}

/* ── 表单布局间距 ── */
QFormLayout {{
    spacing: 10px;
}}

/* ── 幻灯片区域 ── */
QLabel#slide_display {{
    background-color: {BG2};
    border: 1px solid {BORDER};
    border-radius: 6px;
    color: {TEXT3};
}}
QLabel#lbl_slide_index {{
    font-size: 14px;
    font-weight: bold;
    color: {TEXT};
    letter-spacing: 0.5px;
}}

/* ── 录制控制条：阶段提示 + 计时 ── */
QLabel#lbl_hint {{
    color: {TEXT3};
    font-size: 12px;
    padding-left: 4px;
}}
QLabel#lbl_timer {{
    color: {ACCENT};
    font-size: 15px;
    font-weight: bold;
    font-family: "Consolas", "Menlo", monospace;
    padding-left: 12px;
    padding-right: 4px;
}}

/* ── 预览画面 ── */
QLabel#preview_widget {{
    background-color: {BG0};
    border: 1px solid {BORDER};
    border-radius: 6px;
}}
"""
