# README_FOR_AGENT.md

> 面向 **AI Agent / 自动化脚本** 的接入手册。
> 目标：让 Agent 能确定性完成「环境配置 → 启动 → 手机摄像头 adb 接入」全流程，无需人工介入。
>
> 人类读者请改用 [`README.md`](./README.md)，本文件只服务机器执行。

---

## 0. 速览（TL;DR）

```bash
# 1) 装依赖
pip install -r requirements.txt
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121   # 必须补装，见 §3.2

# 2) 启动
python main.py

# 3) 手机摄像头接入（adb）
set ADB=<ROOT>\ADB\scrcpy\scrcpy-win64-v4.1\adb.exe
%ADB% install -r <ROOT>\ADB\NativeCam\build\nativecam.apk
%ADB% shell pm grant com.nativecam.bridge android.permission.CAMERA
%ADB% shell am start-foreground-service -n com.nativecam.bridge/.CameraService
%ADB% forward tcp:8888 tcp:8888
python <ROOT>\ADB\NativeCam\pc\nativecam.py info     # 验证：返回 JSON 即成功
```

实测通过环境：**Python 3.11.7 / torch 2.5.1+cu121 / CUDA True / cv2 4.13.0**。

---

## 1. 项目是什么

CourseRecorder 是桌面录课工具：摄像头对准投影幕布 → 自动梯形矫正 → 自动提取幻灯片 → 实时 ASR 转写 → 可选翻译 → 生成 Markdown 笔记。

**Agent 必须分清的四类视频源**（`src/core/media_source.py` 的 `create_video_source`）：

| `type` | 说明 | 协议 |
|--------|------|------|
| `camera` | 本地/USB 摄像头，OpenCV `VideoCapture` + DirectShow/MSMF | 底层为 **UVC** 驱动 |
| `nativecam` | **安卓手机当高清摄像头** | **adb USB 隧道 + HTTP**（**非 UVC**） |
| `file` | 本地视频文件回放 | — |
| `none` | 不录画面，只做音频转写 | — |

> ⚠️ **手机摄像头不是 UVC 设备**。PC 端不会把它当摄像头打开，而是：
> `adb forward tcp:8888 tcp:8888` 建立 USB 隧道 → 向 `http://127.0.0.1:8888/frame` 拉 JPEG 帧。
> **不要**尝试用 `cv2.VideoCapture(index)` 去枚举手机，那是 `camera` 类型的路径。

---

## 2. 环境硬约束

| 项 | 要求 | 说明 |
|----|------|------|
| 操作系统 | **Windows 10/11** | 采集强依赖 DirectShow（`CAP_DSHOW`）与 COM 初始化；Linux/macOS 未完整测试 |
| Python | 3.9+（**实测 3.11.7**） | 代码含 `X \| Y` 注解但均有 `from __future__ import annotations`，3.9 可跑；推荐 3.11 |
| GPU | 可选 NVIDIA + CUDA | 有则转写/翻译自动走 CUDA；无则降级 CPU |
| 手机（可选） | Android 6.0+（API 23+） | 需开启「开发者选项 → USB 调试」 |

---

## 3. 依赖安装

### 3.1 标准依赖

```bash
cd <ROOT>
python -m venv .venv && .venv\Scripts\activate     # 推荐隔离，也可 conda
pip install -r requirements.txt
```

若下载慢，追加清华源：`-i https://pypi.tuna.tsinghua.edu.cn/simple`

`requirements.txt` 内容分组：

| 用途 | 包 |
|------|-----|
| GUI | `PyQt5` |
| 图像/视频 | `opencv-python`、`numpy`、`Pillow`、`scipy`、`imagehash` |
| 音频采集 | `soundcard`、`pyaudio`、`soundfile`、`webrtcvad` |
| ASR 转写 | `openai-whisper`、`funasr`、`modelscope` |
| 翻译（Qwen3 int4） | `transformers`、`accelerate`、`bitsandbytes`、`sentencepiece`、`protobuf` |
| 打包（可选） | `PyInstaller` |

### 3.2 ⚠️ 已知的 requirements.txt 缺口：缺少 `torch`

`requirements.txt` **没有显式声明 `torch`**，但代码强依赖它：

- `src/core/translator.py` 直接 `import torch`（`torch.cuda.is_available()`、`torch.float16`、`torch.cuda.mem_get_info`）
- `bitsandbytes` 量化依赖与 torch 严格版本匹配
- `funasr` 虽会传递依赖 torch，但**默认装的是 CPU 版**，无法用 GPU

因此 Agent **必须**在装完 requirements.txt 后补装 torch，且要与本机 CUDA 匹配：

```bash
# NVIDIA GPU（推荐，实测 cu121）
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121

# 纯 CPU（无 N 卡时）
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
```

验证：

```bash
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
# 期望输出类似：2.5.1+cu121 True
```

> 建议把 `torch` 补进 `requirements.txt`，否则新环境复现会踩坑（GPU 静默失效或 bitsandbytes 报错）。

### 3.3 首次运行的模型下载

- **Whisper**：首次运行自动下载到 `<ROOT>/models/`（几百 MB ~ 数 GB，取决于模型大小）
- **SenseVoice**：`funasr` 首次加载 `iic/SenseVoiceSmall`（约 254MB）经 `modelscope` 下载
- **Qwen3 翻译**：**默认关闭**，开启后才下载 `Qwen/Qwen3-1.7B`

Agent 若需离线/预下载，提前准备上述缓存目录可避免首次启动长时间卡住。

---

## 4. 启动与验证

```bash
# GUI（主入口）
python main.py

# 或 CLI
python cli.py

# Windows 一键脚本
run.bat          # cmd
powershell -File run.ps1
```

**Agent 验证启动成功的判据**：GUI 窗口出现且状态栏无 `⚠` 报错；或 CLI 输出无 traceback。

---

## 5. 配置说明

### 5.1 配置文件位置

| 项 | 路径 |
|----|------|
| 配置文件 | `~/.course_recorder/config.json`（Windows 即 `%USERPROFILE%\.course_recorder\config.json`） |
| 默认输出目录 | `<ROOT>/outputs/`（按 `session_*` 分目录） |
| Whisper 模型缓存 | `<ROOT>/models/`（固定 ASCII 路径，避免中文路径问题） |
| 中转临时目录 | `<ROOT>/_tmp/`（中文/空格路径视频的 ASCII 中转拷贝） |

配置损坏时程序会**静默回退默认值**并打印 `[Config] 读取配置失败`，不会阻止启动——Agent 不要因该文件缺失而判定失败。

### 5.2 常用配置项

| 键 | 默认 | 说明 |
|----|------|------|
| `video_source` | `{"type":"camera","index":0}` | 视频源；手机为 `{"type":"nativecam","port":8888}` |
| `audio_source` | `{"type":"microphone"}` | `microphone` / `loopback`（系统声音）/ `file` |
| `translate_enabled` | **`false`** | 实时翻译开关。**默认关，故 Qwen3 模型不会在启动时预加载** |
| `slide_sensitivity` | 60 | 幻灯片检测灵敏度 0~100，越高越敏感，录制中可调 |
| `output_dir` | `<ROOT>/outputs` | 录制输出目录 |

> 注意：`translate_enabled` 默认 `false` 是**有意设计**（避免开机即占 1.5GB 显存）。
> 若用户抱怨「Qwen3 没预加载」，先检查此项，而不是去改加载逻辑。

Agent 修改配置推荐走 UI 或直接改 JSON（程序下次启动生效），不要在代码里硬改默认值。

---

## 6. 手机摄像头：adb 完整接入流程（重点）

### 6.1 前置条件

1. 手机开启 **开发者选项 → USB 调试**（关于本机连点版本号 7 次开启开发者选项）
2. USB 数据线连接电脑（**必须是数据线，不能是纯充电线**）
3. 手机屏幕弹出「允许 USB 调试？」→ 勾选一律允许并确认
4. 项目自带 adb，无需系统安装：`<ROOT>\ADB\scrcpy\scrcpy-win64-v4.1\adb.exe`

### 6.2 关键常量（Agent 直接引用，勿改）

| 常量 | 值 |
|------|-----|
| adb 路径 | `<ROOT>\ADB\scrcpy\scrcpy-win64-v4.1\adb.exe` |
| APK 路径 | `<ROOT>\ADB\NativeCam\build\nativecam.apk` |
| 包名 | `com.nativecam.bridge` |
| 前台服务 | `com.nativecam.bridge/.CameraService` |
| 主 Activity | `com.nativecam.bridge/.MainActivity` |
| 端口 | `8888` |
| 权限 | `android.permission.CAMERA` |
| PC 客户端 | `<ROOT>\ADB\NativeCam\pc\nativecam.py` |

### 6.3 接入步骤（按顺序执行，每步都可验证）

```bash
set ROOT=<项目根目录>
set ADB=%ROOT%\ADB\scrcpy\scrcpy-win64-v4.1\adb.exe

:: 步骤 0 —— 确认设备已连接且已授权
%ADB% devices
```
期望输出 `XXXXXX	device`。
- 若显示 `unauthorized` → 手机未授权，需用户点允许
- 若显示 `offline` → 重新插拔数据线
- 若为空 → 线/调试开关问题，终止流程并报告用户

```bash
:: 步骤 1 —— 安装 APK（-r 覆盖安装，保留数据）
%ADB% install -r %ROOT%\ADB\NativeCam\build\nativecam.apk
```
期望含 `Success`。

```bash
:: 步骤 2 —— 授予相机权限（避免 App 内弹窗阻塞）
%ADB% shell pm grant com.nativecam.bridge android.permission.CAMERA
```
无输出即成功（Unix 惯例）。

```bash
:: 步骤 3 —— 启动前台服务（采集 + 本机 HTTP 服务）
%ADB% shell am start-foreground-service -n com.nativecam.bridge/.CameraService
```

```bash
:: 步骤 4 ——（可选）打开 App 界面，手机上可看状态/调参
%ADB% shell am start -n com.nativecam.bridge/.MainActivity
```

```bash
:: 步骤 5 —— 建立 USB 端口转发（关键，不能漏）
%ADB% forward tcp:8888 tcp:8888
```

```bash
:: 步骤 6 —— 验证
python %ROOT%\ADB\NativeCam\pc\nativecam.py info
```
返回 JSON 且含 `width`/`height`/`fps` 即**接入成功**。

### 6.4 一键等价物

```bash
python %ROOT%\ADB\NativeCam\pc\nativecam.py info --grant --start
```
自动完成「授权相机 + 启动服务」，适合 Agent 恢复场景。

### 6.5 在 GUI 中启用

主界面顶部下拉框选择 **手机摄像头** 即可。程序内部走 `connect_nativecam()`（`src/core/nativecam_source.py`）自动完成：找 adb → 检测设备 → forward → 按需装 APK → 授权 → 启服务 → 等 `/info` 就绪（相机被策略禁用时自动 restart 一次）。**即 GUI 路径下 Agent 无需手动敲上述命令**。

### 6.6 重新构建 APK（改了 Java 源码才需要）

预编译 APK 已存在于 `build/nativecam.apk`，**通常无需重建**。仅当修改了 `src/com/nativecam/bridge/*.java` 才需：

```bash
cd <ROOT>\ADB\NativeCam
build.bat
```

构建链（纯命令行，无需 Android Studio/Gradle）：keystore → aapt2 link → javac → jar+d8 → zipalign+apksigner。

工具链已内置，路径硬编码在 `build.bat`：

| 工具 | 路径 |
|------|------|
| JDK 17 | `<ROOT>\ADB\jdk\jdk-17.0.20.1+1` |
| Android SDK | `<ROOT>\ADB\android-sdk`（build-tools 34.0.0 + platform android-30） |

### 6.7 手机端可调参数（HTTP `/setcam`）

| 参数 | 说明 |
|------|------|
| `focus=<屈光度>` | 手动对焦距离，屈光度 = 1/米（`focus=2.0` → 50cm），自动切 AF=OFF |
| `af=auto` / `af=macro` | 恢复自动对焦 / 微距 |
| `iso=<值>` | ISO，需配合 AE OFF |
| `reset=1` | 恢复全部默认 |

GUI 入口：**相机菜单 → 手机摄像头配置...**（滑块面板，与手机端 App 一致）。

---

## 7. 关键路径速查表

```
<ROOT>/
├── main.py                  # GUI 入口
├── cli.py                   # CLI 入口
├── run.bat / run.ps1        # 一键启动
├── requirements.txt         # 依赖（缺 torch，见 §3.2）
├── CourseRecorder.spec      # PyInstaller 配置
├── outputs/                 # 录制输出（session_* 分目录）
├── models/                  # Whisper 模型缓存
├── _tmp/                    # 中文路径视频中转
├── src/
│   ├── app.py               # 路径常量定义（APP_DIR/USER_DIR 等）
│   ├── core/
│   │   ├── media_source.py      # 视频源抽象 + 摄像头/UVC 处理
│   │   ├── nativecam_source.py  # 手机摄像头（adb/HTTP）
│   │   ├── capture_worker.py    # 采集子进程（进程隔离防 DSHOW 崩溃）
│   │   ├── audio_source.py      # 麦克风/系统回环
│   │   ├── screen_detector.py   # 投影区域检测 + 透视矫正
│   │   ├── slide_extractor.py   # 幻灯片变化检测
│   │   ├── transcriber.py       # Whisper / SenseVoice
│   │   ├── translator.py        # Qwen3 翻译（依赖 torch）
│   │   ├── recorder.py          # 录制调度
│   │   ├── session.py           # 会话与输出管理
│   │   └── config.py            # 配置读写
│   └── ui/                  # PyQt5 界面
└── ADB/
    ├── android-sdk/         # 构建 APK 用
    ├── jdk/                 # JDK 17
    ├── scrcpy/              # ⚠️ 内含 adb.exe，勿删
    └── NativeCam/
        ├── build.bat
        ├── src/com/nativecam/bridge/   # MainActivity.java / CameraService.java
        ├── pc/nativecam.py             # PC 客户端（含 CLI）
        └── build/nativecam.apk         # 预编译产物
```

---

## 8. 故障排查

| 症状 | 原因 | 处理 |
|------|------|------|
| `adb devices` 显示 `unauthorized` | 未授权 USB 调试 | 手机点「允许」，重插 |
| `adb devices` 显示 `offline` | 连接异常 | 重插数据线 |
| `adb devices` 为空 | 线/开关问题 | 换数据线；确认 USB 调试已开 |
| APK 安装报 `INSTALL_FAILED` | 旧签名冲突 | 先 `adb uninstall com.nativecam.bridge` 再安装 |
| `nativecam.py info` 无响应 | 服务未启 / 未 forward | 重跑 §6.3 步骤 3+5，或 `--grant --start` |
| App 显示「服务未响应」 | 服务未以 foreground-service 启动 | 必须用 `am start-foreground-service` |
| 相机 `CAMERA_DISABLED ... disabled by policy` | 重装后临时策略锁定 | `python nativecam.py restart`（无需重装） |
| 摄像头黑屏 | 被其它程序占用 | 关闭相机/会议软件；程序内置自动修复 |
| USB 摄像头帧率低 | YUY2 格式限制 | 程序自动协商 MJPG；仍卡则降分辨率 |
| 转写极慢 | 装了 CPU 版 torch | 按 §3.2 重装 CUDA 版 torch |
| Qwen3 未预加载 | `translate_enabled=false` | 有意设计，设置里开启即可 |
| 非主线程崩 `0xC0000005` | DirectShow 未初始化 COM | 已由 `capture_worker.py` 进程隔离兜底，勿绕过该机制 |

---

## 9. Agent 注意事项（Do / Don't）

**Do**
- 用项目自带 adb（`<ROOT>\ADB\scrcpy\scrcpy-win64-v4.1\adb.exe`），不要依赖系统 PATH
- 用 `adb install -r` 覆盖安装，保留 App 数据
- 每步执行后做判据检查（`device` / `Success` / JSON），失败即中止并报告，不要盲目继续
- 修改配置优先改 `~/.course_recorder/config.json`

**Don't**
- ❌ 不要用 `cv2.VideoCapture` 去枚举/打开手机摄像头（那不是 UVC）
- ❌ 不要在子线程直接操作 `cv2.VideoCapture` 而不 `CoInitialize`（会原生崩溃杀进程）——必须走 `capture_worker.py` 子进程
- ❌ 不要删除 `ADB/scrcpy/`（adb 依赖它）
- ❌ 不要为了让 Qwen3 预加载而改 `translate_enabled` 默认值——那会拖慢所有用户启动
- ❌ 不要把 Whisper 模型缓存放到中文路径（已固定 `<ROOT>/models`）

---

## 10. 许可证

**CC BY-NC-SA 4.0**：允许个人学习/研究/非营利教学与二次开发；**禁止任何商业用途**；衍生作品须同协议开源。详见 [`LICENSE`](./LICENSE)。

录制他人授课内容前须获授课者同意——Agent 在自动化批量录制场景中应确保已获授权。
