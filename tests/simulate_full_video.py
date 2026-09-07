"""全片模拟：用与线上完全相同的 SlideExtractor 逻辑跑完整视频。

与线上流式流程的唯一区别是"不等实时"（不等帧间隔），因此可以在几十秒内
跑完 8 分钟视频，用来快速调参；帧时间戳仍然是真实媒体时间轴，
所以稳定态/冷却逻辑与线上一致。

用法：
    python tests/simulate_full_video.py [sensitivity] [stable_sec] [min_persist_sec] [min_changed_area] [frame_step]
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.app import DEFAULT_TEST_VIDEO
from src.core.screen_detector import ScreenDetector
from src.core.slide_extractor import SlideExtractor

MANUAL_CORNERS = np.array(
    [[272.0, 260.0], [1440.0, 28.0], [1516.0, 840.0], [184.0, 892.0]],
    dtype=np.float32,
)

OUT = Path(__file__).resolve().parents[1] / "_probe"


def main() -> int:
    sens = float(sys.argv[1]) if len(sys.argv) > 1 else 45.0
    stable = float(sys.argv[2]) if len(sys.argv) > 2 else 0.6
    persist = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
    min_area = float(sys.argv[4]) if len(sys.argv) > 4 else 0.5
    step = int(sys.argv[5]) if len(sys.argv) > 5 else 3

    import datetime
    out_dir = OUT / f"sim_slides_{datetime.datetime.now().strftime('%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(str(DEFAULT_TEST_VIDEO))
    if not cap.isOpened():
        print("无法打开视频")
        return 1

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

    det = ScreenDetector(output_size=(1280, 720))
    ext = SlideExtractor(
        out_dir,
        sensitivity=sens,
        cooldown_sec=2.0,
        stable_sec=stable,
        min_persist_sec=persist,
        min_area_ratio=0.15,
        min_changed_area=min_area,
    )

    saved = []
    idx = 0
    processed = 0
    while True:
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        if idx % step != 0:
            idx += 1
            continue
        ts = idx / fps
        warped = det.dewarp(frame, MANUAL_CORNERS)
        if warped is not None:
            info = ext.process(warped, ts)
            if info:
                saved.append(info)
        processed += 1
        idx += 1
    cap.release()

    print(
        f"参数: sensitivity={sens} stable={stable} persist={persist} "
        f"min_changed_area={min_area} frame_step={step}"
    )
    print(f"处理帧数: {processed}/{total}")
    print(f"保存幻灯片数: {len(saved)}")
    for s in saved:
        print(f"  #{s['index']:3d}  {s['timestamp']:7.1f}s  {s['filename']}")

    gaps = [
        saved[i + 1]["timestamp"] - saved[i]["timestamp"]
        for i in range(len(saved) - 1)
    ]
    if gaps:
        print(f"\n相邻幻灯片间隔: 最小={min(gaps):.1f}s 最大={max(gaps):.1f}s 平均={sum(gaps)/len(gaps):.1f}s")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
