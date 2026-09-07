"""诊断工具（仅用于离线测量，不属于应用主流程）。

用途：把整段视频按固定间隔采样，用与线上完全相同的透视矫正 + 特征比较逻辑，
统计"真实出现了多少张不同幻灯片"，并打印每帧与上一张幻灯片之间的各项差异指标，
用来定位漏检/重复保存的阈值问题。

注意：应用主流程是流式处理（视频帧逐帧读取、音频边解码边送 ASR），
本脚本只是把同一套算法在离线采样上跑一遍，用来做对照测量。
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.app import DEFAULT_TEST_VIDEO
from src.core.screen_detector import ScreenDetector
from src.core.slide_extractor import (
    SlideFeatures,
    _changed_area_ratio,
    _cosine_distance,
)

# 与线上一致的手动四角（1920x1080 坐标系）
MANUAL_CORNERS = np.array(
    [[272.0, 260.0], [1440.0, 28.0], [1516.0, 840.0], [184.0, 892.0]],
    dtype=np.float32,
)

SAMPLE_INTERVAL_SEC = 1.0
OUT = Path(__file__).resolve().parents[1] / "_probe"
OUT.mkdir(exist_ok=True)


def main() -> int:
    if not DEFAULT_TEST_VIDEO.exists():
        print(f"视频不存在: {DEFAULT_TEST_VIDEO}")
        return 1

    cap = cv2.VideoCapture(str(DEFAULT_TEST_VIDEO))
    if not cap.isOpened():
        print("无法打开视频")
        return 1

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = total / fps if fps else 0.0
    print(f"fps={fps:.2f} total_frames={total} duration={duration:.1f}s")

    det = ScreenDetector(output_size=(1280, 720))

    ref: SlideFeatures | None = None
    slides: list[dict] = []
    stats: list[dict] = []

    t = 0.0
    idx = 0
    while t < duration:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * fps))
        ok, frame = cap.read()
        if not ok or frame is None:
            break

        warped = det.dewarp(frame, MANUAL_CORNERS)
        if warped is None:
            t += SAMPLE_INTERVAL_SEC
            continue

        gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        feats = SlideFeatures.build(warped, gray)

        if ref is None:
            slides.append({"ts": t, "index": 1})
            ref = feats
            cv2.imwrite(str(OUT / f"tl_{len(slides):03d}_{int(t):05d}.jpg"), warped)
            t += SAMPLE_INTERVAL_SEC
            idx += 1
            continue

        phash_dist = int(ref.phash - feats.phash)
        hist_diff = 1.0 - float(cv2.compareHist(ref.hist, feats.hist, cv2.HISTCMP_CORREL))
        edge_diff = _cosine_distance(ref.edge, feats.edge)
        area_ratio = _changed_area_ratio(ref.struct, feats.struct)

        stats.append(
            {
                "ts": t,
                "phash": phash_dist,
                "hist": hist_diff,
                "edge": edge_diff,
                "area": area_ratio,
                "sharp": sharpness,
            }
        )

        # 宽松判据：只要有两项明显变化就认为是新页（用于估计"真实页数"）
        votes = (
            int(phash_dist > 6)
            + int(hist_diff > 0.08)
            + int(edge_diff > 0.20)
        )
        if votes >= 2:
            slides.append({"ts": t, "index": len(slides) + 1})
            ref = feats
            cv2.imwrite(str(OUT / f"tl_{len(slides):03d}_{int(t):05d}.jpg"), warped)

        t += SAMPLE_INTERVAL_SEC
        idx += 1

    cap.release()

    print(f"\n采样点数: {len(stats)}")
    print(f"估计真实幻灯片数: {len(slides)}")
    print("\n幻灯片起始时间:")
    for s in slides:
        print(f"  #{s['index']:3d}  {s['ts']:.1f}s")

    if stats:
        arr = np.array([[s["phash"] for s in stats]], dtype=np.float32)
        print("\n差异指标统计（与上一张幻灯片比较）:")
        print(f"  phash   mean={np.mean([s['phash'] for s in stats]):.2f} "
              f"max={np.max([s['phash'] for s in stats]):.2f}")
        print(f"  hist    mean={np.mean([s['hist'] for s in stats]):.3f} "
              f"max={np.max([s['hist'] for s in stats]):.3f}")
        print(f"  edge    mean={np.mean([s['edge'] for s in stats]):.3f} "
              f"max={np.max([s['edge'] for s in stats]):.3f}")
        print(f"  area    mean={np.mean([s['area'] for s in stats]):.3f} "
              f"max={np.max([s['area'] for s in stats]):.3f}")

        # 打印变化最明显的 20 个采样点，帮助判断阈值
        top = sorted(stats, key=lambda s: -(s["phash"] + s["edge"] * 50))[:20]
        print("\n差异最大的 20 个采样点:")
        for s in sorted(top, key=lambda x: x["ts"]):
            print(
                f"  t={s['ts']:6.1f}s  phash={s['phash']:3d}  "
                f"hist={s['hist']:.3f}  edge={s['edge']:.3f}  area={s['area']:.2f}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
