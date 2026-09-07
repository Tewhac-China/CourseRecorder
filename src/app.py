"""应用级常量、路径与 QApplication 启动入口。

所有与模型/临时文件相关的路径都刻意保持 ASCII，避免 OpenCV / whisper 在
Windows 下处理中文路径时失败。
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path


def _install_crash_handler():
    """安装 Windows 崩溃处理器，防止 DSHOW 崩溃导致整个程序闪退。"""
    # SEM_FAILCRITICALERRORS: 不弹"找不到 DLL"对话框
    # SEM_NOGPFAULTERRORBOX:  不弹崩溃对话框
    # SEM_NOOPENFILEERRORBOX: 不弹"找不到文件"对话框
    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)

_install_crash_handler()

APP_NAME = "CourseRecorder"

# ---------------------------------------------------------------- 目录布局
# 项目根目录（CourseRecorder/），所有相对路径都基于它解析。
APP_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = APP_DIR / "src"
MODELS_DIR = APP_DIR / "models"          # whisper 模型缓存（ASCII 路径）
OUTPUTS_DIR = APP_DIR / "outputs"        # 默认录制输出目录
TEMP_DIR = APP_DIR / "_tmp"              # 中文/空格路径视频的中转拷贝
TESTS_DIR = APP_DIR / "tests"

# 用户配置目录 ~/.course_recorder/
USER_DIR = Path(os.path.expanduser("~")) / ".course_recorder"
CONFIG_PATH = USER_DIR / "config.json"

# 中转文件名（ASCII，给 OpenCV 用）
TEMP_VIDEO_NAME = "_temp_input.mp4"

# 调试用测试视频（PRD 10 节）
DEFAULT_TEST_VIDEO = Path(
    r"E:\图片\Memories\2026暑假\宁夏旅\Pre\初次英文pre（补录版） .mp4"
)


def ensure_dirs() -> None:
    """创建运行期需要的目录。"""
    for d in (MODELS_DIR, OUTPUTS_DIR, TEMP_DIR, USER_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _load_app_icon():
    """加载应用图标（assets/logo.ico），开发与 PyInstaller 打包环境均可用。

    打包后资源位于 sys._MEIPASS/assets/；开发时位于 APP_DIR/assets/。
    找不到图标时返回 None（不报错，不影响启动）。
    """
    from pathlib import Path as _P

    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(_P(meipass) / "assets" / "logo.ico")
    candidates.append(APP_DIR / "assets" / "logo.ico")
    candidates.append(APP_DIR / "assets" / "logo.png")

    from PyQt5.QtGui import QIcon

    for p in candidates:
        if p.exists():
            icon = QIcon(str(p))
            if not icon.isNull():
                return icon
    return None


def recover_crashed_sessions() -> None:
    """扫描未正常结束的录制会话并恢复数据。

    判断标准：会话目录存在，但没有 session.json（说明 finalize() 没执行完）。
    """
    if not OUTPUTS_DIR.exists():
        return
    for d in sorted(OUTPUTS_DIR.iterdir()):
        if not d.is_dir() or not d.name.startswith("session_"):
            continue
        session_json = d / "session.json"
        if session_json.exists():
            continue  # 已正常结束，跳过
        print(f"[Recover] 发现未结束的会话: {d.name}")
        try:
            # 关闭可能残留的 ffmpeg 进程（不需要，子进程随主进程已退出）
            # 检查有哪些数据文件存在
            has_audio = (d / "audio.raw").exists() or (d / "audio.wav").exists()
            has_video = (d / "raw_video.mkv").exists() or (d / "raw_video.mp4").exists()
            has_transcript = (d / "transcript.jsonl").exists()
            has_slides = list((d / "slides").glob("*.jpg")) if (d / "slides").exists() else []

            if not (has_audio or has_video or has_transcript or has_slides):
                print(f"  空会话，跳过")
                continue

            # 尝试转换 raw PCM → WAV
            raw_path = d / "audio.raw"
            wav_path = d / "audio.wav"
            if raw_path.exists() and not wav_path.exists():
                import wave
                try:
                    with open(str(raw_path), "rb") as rf, wave.open(str(wav_path), "wb") as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)
                        wf.setframerate(16000)
                        while True:
                            chunk = rf.read(65536)
                            if not chunk:
                                break
                            wf.writeframes(chunk)
                    raw_path.unlink()
                    print(f"  音频恢复完成 (PCM→WAV)")
                except Exception as exc:
                    print(f"  音频恢复失败: {exc}")

            # 生成 session.json
            import json
            from datetime import datetime
            created_str = d.name.replace("session_", "").replace("-", "/").replace("_", " ").split("/")[0:3]
            data = {
                "session_name": d.name,
                "created_at": datetime.now().isoformat(),
                "recovered": True,
                "video_source": {},
                "audio_source": {},
                "slide_count": len(has_slides),
                "transcript_count": 0,
                "raw_video": has_video,
            }
            # 统计转写条数
            tpath = d / "transcript.jsonl"
            if tpath.exists():
                try:
                    data["transcript_count"] = sum(1 for _ in tpath.open("r", encoding="utf-8"))
                except Exception:
                    pass
            with session_json.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)

            print(f"  恢复成功: {len(has_slides)} 张幻灯片, {data['transcript_count']} 条转写")
        except Exception as exc:
            print(f"[Recover] 恢复失败 {d.name}: {exc}")


def setup_environment() -> None:
    """在导入 whisper / torch 之前设置好环境变量。"""
    ensure_dirs()
    # whisper 的模型缓存目录（whisper.audio / __init__ 读取这些变量）
    os.environ.setdefault("WHISPER_CACHE", str(MODELS_DIR))
    os.environ.setdefault("XDG_CACHE_HOME", str(MODELS_DIR))
    # 抑制 torch 的重复 thread 警告，避免和 Qt 抢 CPU
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    # 让 ffmpeg 的错误输出保持英文，便于解析
    os.environ.setdefault("AV_LOG_FORCE_COLOR", "0")
    # 绕过系统代理 — Windows 系统代理（Clash/V2Ray 等）会干扰 HTTPS 连接，
    # 导致 Whisper/FunASR/HuggingFace 模型加载卡死或 SSL 错误
    os.environ["no_proxy"] = "*"
    os.environ["NO_PROXY"] = "*"


def run() -> int:
    """启动 GUI 应用，返回进程退出码。"""
    setup_environment()
    recover_crashed_sessions()

    # 保证 `python main.py` 与 `python -m src.app` 都能导入 src 包
    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))

    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QIcon
    from PyQt5.QtWidgets import QApplication

    # 高分屏适配（必须在创建 QApplication 之前）
    try:
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    except Exception:
        pass

    # Windows 任务栏归属：注册独立的 AppUserModelID，让任务栏把本程序
    # 识别为独立应用，显示 CourseRecorder 自己的图标而非 python.exe 的。
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_NAME)
    except Exception:
        pass

    from src.core.config import Config
    from src.ui.main_window import MainWindow
    from src.ui.theme import GLOBAL_QSS

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setStyleSheet(GLOBAL_QSS)

    # 应用图标（窗口 / 任务栏 / Alt-Tab / 托盘区域）
    icon = _load_app_icon()
    if icon is not None:
        app.setWindowIcon(icon)

    config = Config.load()
    window = MainWindow(config)
    window.show()

    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(run())
