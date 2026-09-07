"""测试视频分段录制与拼接功能。"""

import sys
import time
from pathlib import Path

import cv2
import numpy as np

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.session import Session


def create_test_frame(width: int = 640, height: int = 480, color: tuple = (0, 0, 255)) -> np.ndarray:
    """创建一个测试帧。"""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:] = color
    return frame


def test_video_segmentation():
    """测试视频分段录制与拼接。"""
    print("=== 测试视频分段录制与拼接 ===")

    # 创建临时测试目录
    test_dir = Path("test_output")
    test_dir.mkdir(exist_ok=True)

    # 创建 Session 对象
    session = Session(
        base_dir=test_dir,
        video_source={"type": "camera", "index": 0},
        audio_source={"type": "microphone", "name": "test"},
        record_raw_video=True,
    )
    session.create()
    print(f"会话目录: {session.dir}")

    # 模拟录制第一段视频
    print("\n1. 录制第一段视频 (3秒)...")
    fps = 30.0
    frame1 = create_test_frame(color=(0, 0, 255))  # 红色
    session.init_video_writer(frame1, fps)

    for i in range(90):  # 3秒 @ 30fps
        frame = create_test_frame(color=(0, 0, 255))
        # 添加帧号文字
        cv2.putText(frame, f"Frame {i}", (50, 240),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        session.write_video_frame(frame)
    print(f"  已写入 90 帧")

    # 暂停录制
    print("\n2. 暂停录制...")
    session.pause_video_writer()
    session.pause_audio()
    print("  视频分段已保存")

    # 模拟暂停期间（不录制）
    print("\n3. 暂停期间 (2秒)...")
    time.sleep(1)  # 缩短测试时间

    # 继续录制
    print("\n4. 继续录制第二段视频 (3秒)...")
    frame2 = create_test_frame(color=(0, 255, 0))  # 绿色
    session.resume_video_writer(frame2, fps)
    session.resume_audio()

    for i in range(90):  # 3秒 @ 30fps
        frame = create_test_frame(color=(0, 255, 0))
        # 添加帧号文字
        cv2.putText(frame, f"Frame {90 + i}", (50, 240),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        session.write_video_frame(frame)
    print(f"  已写入 90 帧")

    # 停止录制并 finalize
    print("\n5. 停止录制并拼接视频...")
    session_dir = session.finalize()
    print(f"  会话目录: {session_dir}")

    # 验证结果
    final_video = session.dir / "raw_video.mkv"
    if final_video.exists():
        print(f"\n[OK] 视频文件已生成: {final_video}")
        print(f"   文件大小: {final_video.stat().st_size / 1024:.2f} KB")

        # 检查视频时长
        cap = cv2.VideoCapture(str(final_video))
        if cap.isOpened():
            fps = cap.get(cv2.CAP_PROP_FPS)
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            duration = frame_count / fps if fps > 0 else 0
            print(f"   视频时长: {duration:.2f} 秒")
            print(f"   帧数: {frame_count}")
            print(f"   FPS: {fps}")
            cap.release()
    else:
        print(f"\n[ERROR] 视频文件未生成")

    # 检查分段文件是否已清理
    segment_files = list(session.dir.glob("raw_video_*.mkv"))
    if segment_files:
        print(f"\n[WARNING] 分段文件未清理: {segment_files}")
    else:
        print(f"\n[OK] 分段文件已清理")

    # 清理测试文件
    print("\n6. 清理测试文件...")
    import shutil
    shutil.rmtree(test_dir, ignore_errors=True)
    print("  测试文件已清理")

    print("\n=== 测试完成 ===")


if __name__ == "__main__":
    test_video_segmentation()
