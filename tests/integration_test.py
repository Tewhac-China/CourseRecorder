"""集成测试：不启动 UI，直接跑 Recorder 30 秒，验证输出。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from PyQt5.QtCore import QCoreApplication, QTimer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.app import DEFAULT_TEST_VIDEO
from src.core.config import Config
from src.core.recorder import Recorder

TEST_DURATION_SEC = 35


def main() -> int:
    if not DEFAULT_TEST_VIDEO.exists():
        print(f"测试视频不存在: {DEFAULT_TEST_VIDEO}")
        return 1

    app = QCoreApplication(sys.argv)
    config = Config.load()
    config.set("output_dir", str(Path(__file__).resolve().parents[1] / "outputs"))
    config.set("asr_model_size", "base")  # 测试用 base 模型
    config.set("asr_language", "en")       # 英文演讲视频
    config.set("slide_sensitivity", 45)
    # 手动标定屏幕四角（基于测试视频 1920x1080 第 2 秒自动检测结果）
    config.set("manual_corners", [
        [272.0, 260.0],
        [1440.0, 28.0],
        [1516.0, 840.0],
        [184.0, 892.0],
    ])

    recorder = Recorder(config)
    session_dir: str | None = None

    def on_frame(orig, dewarped, ts):
        print(f"\rframe ts={ts:.1f}s", end="", flush=True)

    def on_slide(info):
        print(f"\n[slide] {info['index']} @ {info['timestamp']:.1f}s -> {info['filename']}")

    def on_transcript(seg):
        print(f"\n[asr] [{seg['start']:.1f}s] {seg['text']}")

    def on_finished():
        nonlocal session_dir
        session_dir = recorder.current_session_dir()
        app.quit()

    recorder.frame_updated.connect(on_frame)
    recorder.slide_captured.connect(on_slide)
    recorder.transcript_updated.connect(on_transcript)
    recorder.finished.connect(on_finished)

    vcfg = {"type": "file", "path": str(DEFAULT_TEST_VIDEO), "realtime": True, "speed": 1.0}
    acfg = {"type": "file"}

    print("启动录制...")
    if not recorder.start(vcfg, acfg):
        print("启动失败")
        return 1

    # 定时停止
    QTimer.singleShot(TEST_DURATION_SEC * 1000, recorder.stop)

    app.exec_()

    if session_dir is None:
        session_dir = recorder.current_session_dir()

    if not session_dir:
        print("\n未生成会话目录")
        return 1

    sdir = Path(session_dir)
    slides = sorted((sdir / "slides").glob("slide_*.jpg")) if (sdir / "slides").exists() else []
    transcript = sdir / "transcript.jsonl"
    notes = sdir / "notes.md"

    print(f"\n会话目录: {sdir}")
    print(f"幻灯片数: {len(slides)}")
    print(f"transcript 存在: {transcript.exists()}")
    print(f"notes 存在: {notes.exists()}")

    if len(slides) < 2:
        print("FAIL: 幻灯片少于 2 张")
        return 1
    if not transcript.exists():
        print("FAIL: 未生成 transcript")
        return 1
    if not notes.exists():
        print("FAIL: 未生成 notes")
        return 1

    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
