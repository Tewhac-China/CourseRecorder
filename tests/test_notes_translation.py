"""测试笔记翻译功能。"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.session import Session


def test_notes_translation():
    """测试笔记翻译功能。"""
    print("=== 测试笔记翻译功能 ===")

    # 创建临时测试目录
    test_dir = Path("test_output")
    test_dir.mkdir(exist_ok=True)

    # 创建 Session 对象
    session = Session(
        base_dir=test_dir,
        video_source={"type": "camera", "index": 0},
        audio_source={"type": "microphone", "name": "test"},
        record_raw_video=False,  # 不录制视频，只测试笔记生成
    )
    session.create()
    print(f"会话目录: {session.dir}")

    # 模拟转写数据（包含翻译）
    transcript = [
        {
            "start": 0.0,
            "end": 2.0,
            "text": "Hello, welcome to the lecture.",
            "translated": "大家好，欢迎来到讲座。"
        },
        {
            "start": 2.0,
            "end": 5.0,
            "text": "Today we will discuss machine learning.",
            "translated": "今天我们将讨论机器学习。"
        },
        {
            "start": 5.0,
            "end": 8.0,
            "text": "Machine learning is a subset of artificial intelligence.",
            "translated": "机器学习是人工智能的一个子集。"
        },
        {
            "start": 8.0,
            "end": 12.0,
            "text": "It allows computers to learn from data.",
            "translated": "它允许计算机从数据中学习。"
        },
    ]

    # 模拟幻灯片数据
    slides = [
        {"timestamp": 0.0, "filename": "slide_001.jpg"},
        {"timestamp": 5.0, "filename": "slide_002.jpg"},
    ]

    # 写入转写数据到 JSONL 文件
    transcript_file = session.dir / "transcript.jsonl"
    import json
    with open(transcript_file, "w", encoding="utf-8") as f:
        for seg in transcript:
            f.write(json.dumps(seg, ensure_ascii=False) + "\n")

    # 写入幻灯片元数据到 JSONL 文件
    slides_file = session.dir / "slides.jsonl"
    with open(slides_file, "w", encoding="utf-8") as f:
        for slide in slides:
            f.write(json.dumps(slide, ensure_ascii=False) + "\n")

    # 测试生成原始笔记
    print("\n1. 生成原始笔记...")
    notes_content = session._build_notes(slides, transcript, include_translation=False)
    notes_path = session.dir / "notes.md"
    notes_path.write_text(notes_content, encoding="utf-8")
    print(f"  原始笔记已生成: {notes_path}")

    # 测试生成带翻译的笔记
    print("\n2. 生成带翻译的笔记...")
    notes_translated_content = session._build_notes(slides, transcript, include_translation=True)
    notes_translated_path = session.dir / "notes_translated.md"
    notes_translated_path.write_text(notes_translated_content, encoding="utf-8")
    print(f"  带翻译的笔记已生成: {notes_translated_path}")

    # 显示笔记内容
    print("\n3. 原始笔记内容:")
    print("-" * 50)
    print(notes_content)
    print("-" * 50)

    print("\n4. 带翻译的笔记内容:")
    print("-" * 50)
    print(notes_translated_content)
    print("-" * 50)

    # 清理测试文件
    print("\n5. 清理测试文件...")
    import shutil
    shutil.rmtree(test_dir, ignore_errors=True)
    print("  测试文件已清理")

    print("\n=== 测试完成 ===")


if __name__ == "__main__":
    test_notes_translation()
