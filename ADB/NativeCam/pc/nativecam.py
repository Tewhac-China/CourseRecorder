# -*- coding: utf-8 -*-
"""
NativeCam PC Client
===================
从 USB 连接的安卓手机获取**摄像头最高分辨率**画面 (静态 JPEG 通道, 如 4032x3024),
供二次开发使用。数据经 adb forward 走 USB, 不走 WiFi。

重要: 本脚本**绝不对画面做降采样**。frame() 返回的 numpy 数组始终是摄像头输出的
原生分辨率 (最高画质)。只有 cv2 显示窗口会为适配屏幕而缩放 (仅显示层, 不影响数据)。

命令行用法:
  python nativecam.py info                     查看分辨率 / FPS / 状态
  python nativecam.py sizes                    列出摄像头支持的 JPEG 尺寸
  python nativecam.py grab -o photo.jpg        抓取一帧(最高分辨率)并保存
  python nativecam.py show                     实时显示
  python nativecam.py serve --port 8080        启动本地 HTTP 服务供其它程序接入

二次开发 (Python):
  from nativecam import NativeCam
  cam = NativeCam()                 # 自动 adb forward
  frame = cam.frame()               # numpy BGR, 原生最高分辨率
  print(frame.shape)                # (3024, 4032, 3)

  for f in cam.frames():            # 持续迭代帧
      cv2.imshow("cam", f)          # 自行缩放显示

依赖: opencv-python, numpy
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

DEFAULT_PORT = 8888          # 手机端 NativeCam 服务端口
DEFAULT_SERVE_PORT = 8080    # PC 本地转发服务端口

ADB_CANDIDATES = [
    os.environ.get("NATIVECAM_ADB", ""),
    "adb",
]


# ---------------------------------------------------------------- 工具
def find_adb():
    for p in ADB_CANDIDATES:
        if not p:
            continue
        try:
            if os.path.isabs(p) and not os.path.exists(p):
                continue
            r = subprocess.run([p, "version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return p
        except Exception:
            continue
    return None


def http_get(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------- 客户端
class NativeCam:
    """从手机 NativeCam App 获取最高分辨率画面."""

    def __init__(self, port=DEFAULT_PORT, adb=None, auto_forward=True, verbose=True):
        self.port = port
        self.base = "http://127.0.0.1:%d" % port
        self.verbose = verbose
        self.adb = adb or find_adb()
        if auto_forward:
            self.forward()

    def _log(self, *a):
        if self.verbose:
            print(*a)

    # --- adb ---
    def forward(self):
        if not self.adb:
            self._log("[!] 未找到 adb, 跳过 forward")
            return False
        subprocess.run([self.adb, "forward", "--remove", "tcp:%d" % self.port],
                       capture_output=True, timeout=5)
        r = subprocess.run([self.adb, "forward", "tcp:%d" % self.port, "tcp:%d" % self.port],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            self._log("[+] adb forward tcp:%d (USB)" % self.port)
            return True
        self._log("[!] adb forward 失败:", r.stderr.strip())
        return False

    def start_service(self, pkg="com.nativecam.bridge"):
        """启动手机端前台服务 (若未运行)."""
        if not self.adb:
            return False
        r = subprocess.run([self.adb, "shell", "am", "start-foreground-service",
                            "-n", pkg + "/.CameraService"],
                           capture_output=True, text=True, timeout=10)
        self._log("[>] start-foreground-service:", r.stdout.strip() or r.stderr.strip())
        return r.returncode == 0

    def grant_camera(self, pkg="com.nativecam.bridge"):
        """授予相机权限 (免 UI 点击)."""
        if not self.adb:
            return False
        r = subprocess.run([self.adb, "shell", "pm", "grant", pkg,
                            "android.permission.CAMERA"],
                           capture_output=True, text=True, timeout=10)
        ok = r.returncode == 0
        self._log("[+] CAMERA 权限已授予" if ok else "[!] grant 失败")
        return ok

    # --- 数据 ---
    def info(self):
        try:
            return json.loads(http_get(self.base + "/info", timeout=5))
        except Exception as e:
            return {"status": "error", "lastError": str(e)}

    def sizes(self):
        try:
            return json.loads(http_get(self.base + "/sizes", timeout=5))
        except Exception as e:
            return ["error: %s" % e]

    # --- 远程控制 ---
    def ctrl(self, path):
        """请求一个控制接口, 返回解析后的 JSON 或原始文本."""
        try:
            data = http_get(self.base + path, timeout=8)
            try:
                return json.loads(data)
            except Exception:
                return data.decode("utf-8", "replace")
        except Exception as e:
            return {"result": "error", "error": str(e)}

    def stop(self):
        return self.ctrl("/stop")

    def restart(self):
        return self.ctrl("/restart")

    def setres(self, size):
        """size 形如 '1920x1080'; size='auto' 恢复最高分辨率."""
        q = "auto=1" if size == "auto" else "size=" + str(size)
        return self.ctrl("/setres?" + q)

    def setquality(self, q):
        return self.ctrl("/setquality?q=%s" % q)

    def raw_jpeg(self, timeout=20):
        """返回原始 JPEG 字节 (最高分辨率, 未处理)."""
        return http_get(self.base + "/frame", timeout=timeout)

    def frame(self, timeout=20):
        """返回 numpy BGR 帧, 原生最高分辨率, 不缩放."""
        jpg = self.raw_jpeg(timeout)
        arr = np.frombuffer(jpg, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("JPEG 解码失败")
        return img

    def frames(self, timeout=20):
        """持续生成帧 (生成器)."""
        while True:
            yield self.frame(timeout)

    def save(self, path, timeout=20):
        """抓取一帧并保存为图片 (原生分辨率)."""
        img = self.frame(timeout)
        ok = cv2.imwrite(path, img, [int(cv2.IMWRITE_JPEG_QUALITY), 100])
        if not ok:
            raise RuntimeError("保存失败: " + path)
        self._log("[+] 已保存 %s  (%dx%d)" % (path, img.shape[1], img.shape[0]))
        return img

    # --- 显示 (显示层缩放, 不改变数据分辨率) ---
    def show(self, win="NativeCam (ESC/q 退出)", max_w=1600):
        info = self.info()
        print("[*] 摄像头输出: %sx%s  (显示窗口会缩放以适配屏幕, 数据仍是原生分辨率)"
              % (info.get("width"), info.get("height")))
        cv2.namedWindow(win, cv2.WINDOW_NORMAL)
        t0, n = time.time(), 0
        for f in self.frames():
            n += 1
            h, w = f.shape[:2]
            disp = f
            if w > max_w:
                disp = cv2.resize(f, (max_w, int(h * max_w / w)),
                                  interpolation=cv2.INTER_AREA)
            fps = n / (time.time() - t0) if time.time() > t0 else 0
            cv2.putText(disp, "%dx%d  %.1f fps  (native)" % (w, h, fps),
                        (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)
            cv2.imshow(win, disp)
            k = cv2.waitKey(1) & 0xFF
            if k in (27, ord('q')):
                break
        cv2.destroyAllWindows()

    # --- 本地转发服务 (供其它程序二次开发) ---
    def serve(self, port=DEFAULT_SERVE_PORT):
        # 防止本地端口与手机端口相同: 否则会请求到自己形成死锁
        if port == self.port:
            raise SystemExit(
                "[X] 本地服务端口(%d) 不能与手机端口(%d)相同, 否则会请求到自己导致死锁。\n"
                "    正确用法: nativecam.py serve --port %d --serve-port %d"
                % (port, self.port, self.port, DEFAULT_SERVE_PORT))
        cam = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, ct, data):
                self.send_response(code)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                p = self.path.split("?")[0]
                if p == "/frame":
                    try:
                        self._send(200, "image/jpeg", cam.raw_jpeg())
                    except Exception as e:
                        self._send(503, "text/plain", str(e).encode())
                elif p == "/info":
                    self._send(200, "application/json",
                               json.dumps(cam.info(), ensure_ascii=False, indent=2).encode())
                elif p == "/sizes":
                    self._send(200, "application/json",
                               json.dumps(cam.sizes(), ensure_ascii=False, indent=2).encode())
                elif p == "/stream":
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "multipart/x-mixed-replace; boundary=frame")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    last = None
                    try:
                        while True:
                            jpg = cam.raw_jpeg()
                            if jpg != last:
                                self.wfile.write(
                                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                    + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                                self.wfile.flush()
                                last = jpg
                            time.sleep(0.02)
                    except (BrokenPipeError, ConnectionResetError, OSError, Exception):
                        pass
                else:
                    html = ("""<html><head><meta charset="utf-8"><title>NativeCam PC</title>
<style>body{background:#1e1e1e;color:#ddd;font-family:sans-serif;padding:24px}
code{background:#000;padding:2px 6px;color:#dcdcaa;display:inline-block;margin:2px 0}</style>
</head><body><h1>NativeCam PC 转发服务</h1>
<p>本地端口 %(p)d &larr; 手机端口 %(src)d (USB)</p>
<div><code>GET /frame</code> 单帧 JPEG (原生最高分辨率)</div>
<div><code>GET /stream</code> MJPEG 流</div>
<div><code>GET /info</code> 状态 JSON</div>
<div><code>GET /sizes</code> 支持的尺寸</div>
<pre># 其它程序接入
import cv2
cap = cv2.VideoCapture("http://127.0.0.1:%(p)d/stream")
ok, frame = cap.read()</pre>
</body></html>""" % {"p": port, "src": cam.port}).encode("utf-8")
                    self._send(200, "text/html; charset=utf-8", html)

        srv = ThreadingHTTPServer(("0.0.0.0", port), H)
        print("[+] 本地转发服务: http://127.0.0.1:%d/" % port)
        print("    /frame  单帧(原生分辨率)   /stream  MJPEG   /info  /sizes")
        print("[*] Ctrl+C 停止")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n[*] 停止")


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="NativeCam PC 客户端 - 获取手机摄像头最高分辨率画面")
    ap.add_argument("cmd", nargs="?", default="info",
                    choices=["info", "sizes", "grab", "show", "serve",
                             "stop", "restart", "setres", "setquality"],
                    help="info/sizes/grab/show/serve/stop/restart/setres/setquality")
    ap.add_argument("-o", "--out", default="photo.jpg", help="grab 保存路径")
    ap.add_argument("--size", default=None, help="setres 用: 1920x1080 或 auto")
    ap.add_argument("--quality", type=int, default=None, help="setquality 用: 1-100")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="手机端端口 (默认 8888)")
    ap.add_argument("--serve-port", type=int, default=DEFAULT_SERVE_PORT, help="serve 本地端口")
    ap.add_argument("--start", action="store_true", help="启动手机端服务")
    ap.add_argument("--grant", action="store_true", help="授予相机权限")
    args = ap.parse_args()

    cam = NativeCam(port=args.port)
    if args.grant:
        cam.grant_camera()
    if args.start:
        cam.start_service()
        time.sleep(2)

    if args.cmd == "info":
        i = cam.info()
        print(json.dumps(i, ensure_ascii=False, indent=2))
        if i.get("status") != "streaming":
            print("\n[!] 服务未就绪。可尝试:")
            print("    python nativecam.py info --grant --start")
    elif args.cmd == "sizes":
        print(json.dumps(cam.sizes(), ensure_ascii=False, indent=2))
    elif args.cmd == "grab":
        cam.save(args.out)
    elif args.cmd == "show":
        cam.show()
    elif args.cmd == "serve":
        cam.serve(args.serve_port)
    elif args.cmd == "stop":
        print(json.dumps(cam.stop(), ensure_ascii=False, indent=2))
    elif args.cmd == "restart":
        print(json.dumps(cam.restart(), ensure_ascii=False, indent=2))
        time.sleep(3)
        print(json.dumps(cam.info(), ensure_ascii=False, indent=2))
    elif args.cmd == "setres":
        size = args.size or "auto"
        print(json.dumps(cam.setres(size), ensure_ascii=False, indent=2))
        time.sleep(2)
        print(json.dumps(cam.info(), ensure_ascii=False, indent=2))
    elif args.cmd == "setquality":
        if args.quality is None:
            ap.error("--quality N 必须提供 (1-100)")
        print(json.dumps(cam.setquality(args.quality), ensure_ascii=False, indent=2))
        time.sleep(1)
        print(json.dumps(cam.info(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
