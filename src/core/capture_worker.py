# -*- coding: utf-8 -*-
"""摄像头采集子进程：在独立进程内打开摄像头并持续把帧以 JPEG 流回传。

进程隔离的目的：OpenCV 的 DSHOW 后端在个别摄像头/驱动下会原生访问越界
(0xC0000005) 崩溃，且无法被 Python 捕获。把采集放进子进程后，原生崩溃只
会杀死本子进程（管道随之关闭），父进程（GUI）检测到后自动重连/换后端，
绝不会闪退。

帧格式（写往 stdout）：4 字节小端长度 N + N 字节 JPEG。

分辨率/像素格式协商：部分廉价 USB/内置摄像头固件脆弱，直接请求最高清
(如 2592x1944) 会输出黑屏或丢色度(灰度)。故按阶梯尝试：
  (请求分辨率, 1920x1080, 1280x720, 640x480) × (默认格式, MJPG)
首个能出非黑、非灰度帧的组合即采用。
"""
import os
import sys
import argparse
import struct
import time

os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")

import cv2
import numpy as np


def _is_grayscale(frame: np.ndarray) -> bool:
    """检测帧是否丢失色度（BGR 三通道无差异 → 灰度画面）。"""
    b, g, r = (frame[..., i].astype(np.int16) for i in range(3))
    return float(np.mean(np.abs(r - g)) + np.mean(np.abs(g - b))) < 0.5


def _try_res(cap, cw, ch, fps, mjpg) -> bool:
    MJPG = cv2.VideoWriter_fourcc(*"MJPG")
    YUY2 = cv2.VideoWriter_fourcc(*"YUY2")
    # 重置 FOURCC：上次 MJPG 尝试失败后可能残留状态，导致后续格式也打不开
    cap.set(cv2.CAP_PROP_FOURCC, YUY2 if not mjpg else MJPG)
    if fps > 0:
        cap.set(cv2.CAP_PROP_FPS, fps)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cw)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, ch)
    if mjpg:
        # 分辨率/帧率协商会把 FOURCC 重置，MJPG 必须是首次 read 前最后设置
        cap.set(cv2.CAP_PROP_FOURCC, MJPG)
    for _ in range(3):
        ok, f = cap.read()
        if ok and f is not None and f.size > 0 and f.max() > 0:
            if not (mjpg and _is_grayscale(f)):
                return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--backend", type=int, default=int(cv2.CAP_DSHOW))
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--grab-flush", type=int, default=1,
                    help="每次读帧前先 grab 的次数（丢弃旧缓冲帧，降低延迟）")
    args = ap.parse_args()

    # COM 初始化（子进程主线程，DSHOW 必须）
    try:
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 0x2)
    except Exception:
        pass

    cap = cv2.VideoCapture(args.index, args.backend)
    if cap is None or not cap.isOpened():
        # 打开失败：退出，父进程会检测到管道关闭并重试其它后端
        sys.stderr.write("OPEN_FAIL\n")
        sys.stderr.flush()
        return
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass

    w, h, fps = args.width, args.height, args.fps
    ok_cfg = False
    for (cw, ch) in [(w, h), (1920, 1080), (1280, 720), (640, 480)]:
        # MJPG 优先：廉价 USB 摄像头（如 CM200）在 YUY2 下只有 10fps，
        # MJPG 可达 30fps。_try_res 已内置 FOURCC 重置，失败不会污染后续尝试。
        if _try_res(cap, cw, ch, fps, True):
            ok_cfg = True
            break
        if _try_res(cap, cw, ch, fps, False):
            ok_cfg = True
            break
    if not ok_cfg:
        # 兜底：用驱动默认格式再读一次，能出画面即可（有画面 > 黑屏）
        for _ in range(3):
            ok, f = cap.read()
            if ok and f is not None and f.size > 0 and f.max() > 0:
                ok_cfg = True
                break

    # 首帧预热：丢弃前几帧（DSHOW 驱动初始化阶段可能输出黑帧/不稳定帧）
    grab_flush = max(0, args.grab_flush)
    warmup = 3
    for _ in range(warmup):
        for _ in range(grab_flush):
            cap.grab()
        cap.read()

    out = sys.stdout.buffer
    while True:
        # grab-flush：先 grab N 次丢弃 DSHOW 内部缓冲的旧帧，
        # 再 read 取最新帧，显著降低延迟（消除果冻效应）。
        for _ in range(grab_flush):
            cap.grab()
        ok, frame = cap.read()
        if not ok or frame is None or frame.size == 0 or len(frame.shape) < 2:
            time.sleep(0.01)
            continue
        ok_j, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        if not ok_j:
            continue
        data = buf.tobytes()
        out.write(struct.pack("<I", len(data)))
        out.write(data)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.stderr.write("WORKER_ERROR:%s\n" % e)
        sys.stderr.flush()
