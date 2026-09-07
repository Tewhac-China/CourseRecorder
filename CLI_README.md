# CourseRecorder CLI 调试工具

绕过 Qt GUI 直接测试功能，用于调试摄像头 / 手机连接问题。

## 使用方法

### 测试手机摄像头 (NativeCam USB)

```bash
# 完整测试（自动连接、捕获帧）
python cli.py nativecam --test --frames 3

# 仅测试连接（不捕获帧）
python cli.py nativecam
```

全自动完成：定位内置 adb → 检测 USB 手机 → adb forward → 安装 APK（若未装）→
授予相机权限 → 启动手机端服务 → 等待画面就绪。

### 列出所有摄像头

```bash
python cli.py cameras
```

### 测试 ADB 连接

```bash
python cli.py adb
```

### 快捷脚本

- `test_all.bat` - 完整测试所有功能（ADB / 摄像头 / 手机摄像头）

## 功能说明

### 手机摄像头测试流程 (NativeCam USB)

1. 查找 adb.exe（优先项目内置 `ADB/scrcpy/.../adb.exe`）
2. 检测 USB 手机是否连接并授权
3. 全自动连接（forward → 安装/授权 → 启动服务）
4. 帧捕获测试（可选）

### 输出信息

- **连接结果**: 成功/失败
- **分辨率**: 手机摄像头输出的最大分辨率
- **帧信息**: 实际拉取到的帧尺寸与成功数

## 故障排除

### adb 未找到

```
ADB: 未找到
```

确认项目 `ADB` 目录完整，或设置环境变量 `NATIVECAM_ADB` 指向 adb.exe。

### ADB 设备未连接

```
状态: 未连接
信息: 未检测到 USB 手机
```

检查：
1. USB 线已连接
2. 手机已开启 USB 调试
3. 手机上已授权此电脑
4. 若提示未授权：在手机上允许 USB 调试

### 手机端服务未就绪

```
消息: 手机端 NativeCam 服务未就绪: ...
```

多为手机端 App 首次运行或相机被策略锁定，重试会自动 `restart` 手机端服务；
也可在手机上打开 NativeCam App 查看状态。

## 代码结构

- `cli.py` - CLI 主程序
- `src/core/media_source.py` - 物理摄像头扫描/检测核心（PnP 限定范围快速扫描）
- `src/core/nativecam_source.py` - 手机摄像头 (NativeCam USB) 核心
  - `find_adb()` - 定位 adb.exe
  - `adb_device_id()` - 检测 USB 手机
  - `connect_nativecam()` - 完整自动连接流程
  - `NativeCamSource` - MediaSource 视频源（原生最高分辨率取帧）
