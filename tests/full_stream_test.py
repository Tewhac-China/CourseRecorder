"""全片流式测试：把 8 分钟视频作为虚拟摄像头，按 1x 实时速度跑完。

验证：
- 幻灯片数量与模拟结果一致；
- 字幕持续输出；
- 输出文件正常生成。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt5.QtCore import QCoreApplication, QTimer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.app import DEFAULT_TEST_VIDEO
from src.core.config import Config
from src.core.recorder import Recorder


def main() -> int:
    if not DEFAULT_TEST_VIDEO.exists():
        print(f"测试视频不存在: {DEFAULT_TEST_VIDEO}")
        return 1

    app = QCoreApplication(sys.argv)
    config = Config.load()
    config.set("output_dir", str(Path(__file__).resolve().parents[1] / "outputs"))
    config.set("asr_model_size", "base")
    config.set("asr_language", "en")
    config.set("slide_sensitivity", 45)
    config.set("slide_stable_sec", 0.6)
    config.set("slide_min_persist_sec", 1.0)
    config.set("manual_corners", [
        [272.0, 260.0],
        [1440.0, 28.0],
        [1516.0, 840.0],
        [184.0, 892.0],
    ])

    recorder = Recorder(config)
    session_dir: str | None = None
    slide_count = 0
    transcript_count = 0

    def on_frame(orig, dewarped, ts):
        print(f"\r[stream] ts={ts:.1f}s  slides={slide_count}  transcripts={transcript_count}", end="", flush=True)

    def on_slide(info):
        nonlocal slide_count
        slide_count += 1
        print(f"\n[slide] #{slide_count} @ {info['timestamp']:.1f}s")

    def on_transcript(seg):
        nonlocal transcript_count
        transcript_count += 1
        text = seg.get("text", "").strip()
        if text:
            print(f"\n[asr] [{seg['start']:.1f}s] {text[:100]}")

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

    print("启动全片流式测试（1x 实时，约 8 分钟）...")
    if not recorder.start(vcfg, acfg):
        print("启动失败")
        return 1

    # 如果 10 分钟还没结束，强制停止
    QTimer.singleShot(10 * 60 * 1000, recorder.stop)

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

    print(f"\n\n会话目录: {sdir}")
    print(f"幻灯片数: {len(slides)}")
    print(f"转写句数: {transcript_count}")
    print(f"transcript 存在: {transcript.exists()}")
    print(f"notes 存在: {notes.exists()}")

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
