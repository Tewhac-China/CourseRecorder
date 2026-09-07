"""测试笔记翻译功能（模拟真实场景）。"""

import sys
import json
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.session import Session


def test_notes_translation_real():
    """测试笔记翻译功能（模拟真实场景）。"""
    print("=== 测试笔记翻译功能（模拟真实场景） ===")

    # 创建临时测试目录
    test_dir = Path("test_output")
    test_dir.mkdir(exist_ok=True)

    # 创建 Session 对象
    session = Session(
        base_dir=test_dir,
        video_source={"type": "camera", "index": 0},
        audio_source={"type": "microphone", "name": "test"},
        record_raw_video=False,
    )
    session.create()
    print(f"会话目录: {session.dir}")

    # 模拟转写数据（包含翻译）- 模拟真实 ASR 输出
    transcript = [
        {
            "start": 0.0,
            "end": 3.5,
            "text": "Good morning everyone, welcome to today's lecture on machine learning.",
            "translated": "大家好，欢迎参加今天的机器学习讲座。"
        },
        {
            "start": 3.5,
            "end": 7.2,
            "text": "Today we will cover the basics of neural networks.",
            "translated": "今天我们将介绍神经网络的基础知识。"
        },
        {
            "start": 7.2,
            "end": 12.0,
            "text": "Neural networks are inspired by the human brain and consist of layers of interconnected nodes.",
            "translated": "神经网络受人脑启发，由相互连接的节点层组成。"
        },
        {
            "start": 12.0,
            "end": 16.5,
            "text": "Each node processes information and passes it to the next layer.",
            "translated": "每个节点处理信息并将其传递到下一层。"
        },
        {
            "start": 16.5,
            "end": 21.0,
            "text": "The network learns by adjusting the weights of these connections.",
            "translated": "网络通过调整这些连接的权重来学习。"
        },
    ]

    # 模拟幻灯片数据
    slides = [
        {"timestamp": 0.0, "filename": "slide_001.jpg"},
        {"timestamp": 10.0, "filename": "slide_002.jpg"},
    ]

    # 写入转写数据到 JSONL 文件
    transcript_file = session.dir / "transcript.jsonl"
    with open(transcript_file, "w", encoding="utf-8") as f:
        for seg in transcript:
            f.write(json.dumps(seg, ensure_ascii=False) + "\n")

    # 写入幻灯片元数据到 JSONL 文件
    slides_file = session.dir / "slides.jsonl"
    with open(slides_file, "w", encoding="utf-8") as f:
        for slide in slides:
            f.write(json.dumps(slide, ensure_ascii=False) + "\n")

    # 创建幻灯片目录和模拟图片
    slides_dir = session.dir / "slides"
    slides_dir.mkdir(exist_ok=True)
    for slide in slides:
        # 创建空文件模拟幻灯片图片
        (slides_dir / slide["filename"]).touch()

    # 测试 finalize 方法
    print("\n1. 测试 finalize 方法...")
    session_dir = session.finalize()
    print(f"  会话目录: {session_dir}")

    # 检查生成的文件
    notes_path = session.dir / "notes.md"
    notes_translated_path = session.dir / "notes_translated.md"

    print("\n2. 检查生成的文件...")
    if notes_path.exists():
        print(f"  [OK] notes.md 已生成")
        print(f"    文件大小: {notes_path.stat().st_size} 字节")
    else:
        print(f"  [ERROR] notes.md 未生成")

    if notes_translated_path.exists():
        print(f"  [OK] notes_translated.md 已生成")
        print(f"    文件大小: {notes_translated_path.stat().st_size} 字节")
    else:
        print(f"  [ERROR] notes_translated.md 未生成")

    # 显示笔记内容
    print("\n3. notes.md 内容:")
    print("-" * 60)
    if notes_path.exists():
        print(notes_path.read_text(encoding="utf-8"))
    print("-" * 60)

    print("\n4. notes_translated.md 内容:")
    print("-" * 60)
    if notes_translated_path.exists():
        print(notes_translated_path.read_text(encoding="utf-8"))
    print("-" * 60)

    # 清理测试文件
    print("\n5. 清理测试文件...")
    import shutil
    shutil.rmtree(test_dir, ignore_errors=True)
    print("  测试文件已清理")

    print("\n=== 测试完成 ===")


if __name__ == "__main__":
    test_notes_translation_real()
