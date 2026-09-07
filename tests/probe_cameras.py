"""摄像头枚举诊断：对比 DSHOW / MSMF 后端，并探测各索引支持的分辨率。"""

from __future__ import annotations

import time

import cv2

RESOLUTIONS = [
    (3840, 2160),
    (2560, 1440),
    (1920, 1080),
    (1600, 900),
    (1280, 720),
    (1024, 768),
    (800, 600),
    (640, 480),
]

BACKENDS = [
    ("DSHOW", cv2.CAP_DSHOW),
    ("MSMF", cv2.CAP_MSMF),
    ("ANY", None),
]


def try_open(idx: int, backend: int | None):
    t0 = time.perf_counter()
    try:
        cap = cv2.VideoCapture(idx, backend) if backend is not None else cv2.VideoCapture(idx)
        if not cap.isOpened():
            cap.release()
            return None, time.perf_counter() - t0
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.release()
            return None, time.perf_counter() - t0
        return cap, time.perf_counter() - t0
    except Exception:
        return None, time.perf_counter() - t0


def probe_resolutions(cap) -> list[tuple[int, int]]:
    supported = []
    for w, h in RESOLUTIONS:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if (aw, ah) not in supported:
            supported.append((aw, ah))
    return supported


def main() -> int:
    print(f"OpenCV: {cv2.__version__}")
    print()

    for idx in range(8):
        row = []
        working = None
        for bname, backend in BACKENDS:
            cap, dt = try_open(idx, backend)
            if cap is None:
                row.append(f"{bname}: FAIL ({dt*1000:.0f}ms)")
                continue
            h, w = cap.read()[1].shape[:2] if False else (0, 0)
            ok, frame = cap.read()
            if ok and frame is not None:
                h, w = frame.shape[:2]
            fps = cap.get(cv2.CAP_PROP_FPS)
            row.append(f"{bname}: OK {w}x{h} fps={fps:.1f} ({dt*1000:.0f}ms)")
            if working is None:
                working = (bname, backend, cap, w, h)
            else:
                cap.release()
        print(f"index {idx}: " + " | ".join(row))

        if working is not None:
            bname, backend, cap, w, h = working
            supported = probe_resolutions(cap)
            print(f"          resolutions: {supported}")
            cap.release()
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
