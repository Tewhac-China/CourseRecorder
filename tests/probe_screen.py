"""探针：在测试视频的若干采样帧上跑屏幕检测，输出角点与矫正图，用于调参。"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np

from src.core.media_source import FileSource
from src.core.screen_detector import ScreenDetector, QuadStabilizer

VIDEO = Path(r"E:\图片\Memories\2026暑假\宁夏旅\Pre\初次英文pre（补录版） .mp4")
OUT = Path(__file__).resolve().parents[1] / "_probe"
OUT.mkdir(exist_ok=True)


def main() -> int:
    src = FileSource(VIDEO, realtime=False)
    if not src.start():
        print("FAIL: 无法打开视频")
        return 1
    fps = src.get_fps()
    print(f"fps={fps:.2f} size={src.frame_size} frames={src.total_frames} dur={src.duration:.1f}s")

    det = ScreenDetector(output_size=(1280, 720))
    stab = QuadStabilizer(alpha=0.25)

    # 每 10 秒采样一帧
    sample_secs = [float(s) for s in (sys.argv[1:] or [2, 15, 30, 60, 120, 200, 280, 360, 450])]
    results = []
    for sec in sample_secs:
        src.seek_seconds(sec)
        ok, frame, ts = src.read_frame()
        if not ok:
            print(f"{sec:6.1f}s  read FAIL")
            continue
        t0 = time.perf_counter()
        quad = det.auto_detect(frame)
        dt = (time.perf_counter() - t0) * 1000
        smoothed = stab.update(quad, frame.shape)
        if quad is None:
            print(f"{sec:6.1f}s  detect=None  ({dt:.1f} ms)")
            results.append((sec, None, frame))
            continue
        o = det.order_points(quad)
        tl, tr, br, bl = o
        wt = np.linalg.norm(tr - tl)
        wb = np.linalg.norm(br - bl)
        hl = np.linalg.norm(bl - tl)
        hr = np.linalg.norm(br - tr)
        aw, ah = (wt + wb) / 2, (hl + hr) / 2
        area_ratio = abs(cv2.contourArea(o)) / (frame.shape[0] * frame.shape[1])
        print(
            f"{sec:6.1f}s  quad ok ({dt:.1f} ms) aspect={aw/ah:.3f} "
            f"area%={area_ratio*100:.1f} pts={o.astype(int).tolist()}"
        )
        warped = det.dewarp(frame, quad)
        if warped is not None:
            cv2.imwrite(str(OUT / f"dewarp_{int(sec):04d}.jpg"), warped)
        results.append((sec, quad, frame))

    # 画一张带检测框的原图拼图
    tiles = []
    for sec, quad, frame in results:
        if frame is None:
            continue
        vis = ScreenDetector.draw_overlay(frame, quad)
        vis = cv2.resize(vis, (480, 270))
        cv2.putText(vis, f"{sec:.0f}s", (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
        tiles.append(vis)
    if tiles:
        cols = 3
        rows = (len(tiles) + cols - 1) // cols
        canvas = np.zeros((rows * 270, cols * 480, 3), np.uint8)
        for i, t in enumerate(tiles):
            r, c = divmod(i, cols)
            canvas[r * 270:(r + 1) * 270, c * 480:(c + 1) * 480] = t
        cv2.imwrite(str(OUT / "detect_overview.jpg"), canvas)
        print(f"\n拼图已保存: {OUT / 'detect_overview.jpg'}")

    src.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
