# NativeCam

通过 **USB（adb）** 从安卓手机获取摄像头**最高分辨率** JPEG 画面（实测最高 3264×2448），
供 PC 端二次开发使用。数据走 adb forward 的 USB 通道，不经过 WiFi。
所有画面**不做降采样**，始终为摄像头原生输出分辨率。

---

## 工作区目录结构

```
ADB/
├── android-sdk/        # Android SDK（aapt2 / build-tools 34.0.0 / platform android-30）— 构建 APK 用
├── jdk/                # JDK 17 — 编译与打包用
├── scrcpy/             # 内含 adb.exe，本项目的 adb 依赖它，请勿删除
└── NativeCam/          # 本项目
    ├── AndroidManifest.xml
    ├── build.bat       # 一键构建并签名 APK
    ├── src/com/nativecam/bridge/
    │   ├── MainActivity.java   # 手机端 App：状态显示 + 分辨率/画质/重启/停止 控制
    │   └── CameraService.java  # 前台服务：摄像头采集 + 本机 HTTP 服务 (127.0.0.1:8888)
    ├── pc/
    │   ├── nativecam.py  # PC 客户端（命令行 + 二次开发库）
    │   └── start.bat     # PC 端交互菜单
    └── build/           # 构建产物（nativecam.apk 可直接安装）
```

---

## 环境依赖

- **Java**：`jdk/`（JDK 17）
- **Android SDK**：`android-sdk/`（build-tools 34.0.0 + platform android-30）
- **adb**：`scrcpy/scrcpy-win64-v4.1/adb.exe`（脚本已内置该路径候选）
- **Python**：`opencv-python`、`numpy`
  ```bash
  pip install opencv-python numpy
  ```

---

## 构建 APK

双击 `NativeCam/build.bat`（或在命令行运行）。脚本会自动完成：

1. 生成/复用签名 `nativecam.keystore`
2. `aapt2` 链接 `AndroidManifest.xml`
3. `javac` 编译 `src/`
4. `d8` 将 class 转为 `classes.dex`
5. `jar` + `zipalign` + `apksigner` 打包签名

产物：`NativeCam/build/nativecam.apk`
（重跑 `build.bat` 会完整重建，无需手动清理。）

---

## 安装到手机

```bash
adb install -r NativeCam\build\nativecam.apk
adb shell pm grant com.nativecam.bridge android.permission.CAMERA
adb shell am start-foreground-service -n com.nativecam.bridge/.CameraService
adb shell am start -n com.nativecam.bridge/.MainActivity
adb forward tcp:8888 tcp:8888
```

手机上打开 NativeCam App 即可看到实时状态（分辨率 / 帧率 / 帧数）与各项控制。

---

## PC 端使用

```bash
cd NativeCam\pc

python nativecam.py info                      # 查看状态（分辨率/FPS/帧数）
python nativecam.py sizes                     # 列出摄像头支持的 JPEG 尺寸
python nativecam.py grab -o photo.jpg         # 抓取一帧（最高分辨率）并保存
python nativecam.py show                      # 实时显示窗口（ESC/q 退出）
python nativecam.py serve --serve-port 8080   # 本地 HTTP 服务，供其它程序接入
python nativecam.py setres --size 1920x1080   # 切换分辨率（auto 恢复最高）
python nativecam.py setquality --quality 90   # 切换 JPEG 画质（1-100）
python nativecam.py restart                   # 重启手机端相机
python nativecam.py stop                      # 停止手机端服务
python nativecam.py info --grant --start      # 一键：授权相机 + 启动手机端服务
```

也可直接双击 `pc/start.bat` 使用交互菜单。

本地 HTTP 服务（`serve`）提供的接口：

| 路径         | 说明                          |
|--------------|-------------------------------|
| `/frame`     | 单帧 JPEG（原生最高分辨率）   |
| `/stream`    | MJPEG 流                      |
| `/info`      | 状态 JSON                     |
| `/sizes`     | 支持的尺寸 JSON               |

---

## 二次开发（Python）

```python
from nativecam import NativeCam

cam = NativeCam()            # 自动执行 adb forward
frame = cam.frame()          # numpy BGR，始终是摄像头原生最高分辨率
print(frame.shape)           # 例如 (2448, 3264, 3)

for f in cam.frames():       # 持续迭代帧
    # 自行处理 / 缩放显示，不要对 f 做降采样以保留画质
    pass
```

其它程序也可直接访问本地转发服务，例如 OpenCV：
```python
import cv2
cap = cv2.VideoCapture("http://127.0.0.1:8080/stream")
ok, frame = cap.read()
```

---

## 故障排查

- **App 显示“服务未响应”**
  确认手机端服务已用 `am start-foreground-service` 启动，且 PC 已执行
  `adb forward tcp:8888 tcp:8888`。也可在 PC 端运行
  `python nativecam.py info --grant --start` 一键拉起。

- **相机被禁用：`CAMERA_DISABLED (1): ... disabled by policy`**
  多为重装 App 后首次启动的临时策略锁定。执行
  `python nativecam.py restart` 重启手机端服务即可恢复（无需重装）。

- **需要重新构建 APK**
  直接运行 `NativeCam/build.bat`。

---

## 实现要点

- 手机端 `CameraService` 在 `127.0.0.1:8888` 提供本机 HTTP 接口
  （`/info` `/frame` `/sizes` `/setres` `/setquality` `/restart` `/stop`）。
- App 的 `MainActivity` 使用**原生 `Socket`** 直连该服务：Android 在
  targetSdk≥28 下会拦截 `HttpURLConnection` 的明文请求，而底层 Socket
  不受明文（cleartext）策略限制（服务仅监听 127.0.0.1，无中间人风险）。
- 数据通道走 `adb forward` 的 USB，不依赖 WiFi 或网络明文。
