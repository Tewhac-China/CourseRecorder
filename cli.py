"""CourseRecorder CLI 调试模式 - 绕过 Qt GUI 直接测试功能。"""

import argparse


def main():
    parser = argparse.ArgumentParser(description="CourseRecorder CLI 调试工具")
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # 测试手机摄像头（NativeCam USB）
    nc_parser = subparsers.add_parser("nativecam", help="测试手机摄像头（NativeCam USB）连接")
    nc_parser.add_argument("--test", action="store_true", help="测试连接并捕获帧")
    nc_parser.add_argument("--frames", type=int, default=3, help="捕获帧数 (默认3)")

    # 列出摄像头
    subparsers.add_parser("cameras", help="列出所有可用摄像头")

    # 测试 ADB
    subparsers.add_parser("adb", help="测试 ADB 连接")

    args = parser.parse_args()

    if args.command == "nativecam":
        test_nativecam(args.test, args.frames)
    elif args.command == "cameras":
        list_cameras()
    elif args.command == "adb":
        test_adb()
    else:
        parser.print_help()


def list_cameras():
    """列出所有可用摄像头。"""
    print("=" * 60)
    print("摄像头列表")
    print("=" * 60)

    try:
        from src.core.media_source import list_cameras
        cameras = list_cameras()
        if cameras:
            for i, cam in enumerate(cameras):
                print(f"\n[{i+1}] {cam['name']}")
                print(f"    索引: {cam['index']}")
                print(f"    分辨率: {cam['max_width']}x{cam['max_height']}")
                print(f"    后端: {cam['backend']}")
        else:
            print("\n未找到摄像头")
        print("\n提示: 手机摄像头用 `python cli.py nativecam --test` 测试")
    except Exception as e:
        print(f"\n[ERROR] {e}")


def test_nativecam(capture=False, frames=3):
    """测试手机摄像头（NativeCam USB）连接。"""
    print("=" * 60)
    print("手机摄像头 (NativeCam USB) 连接测试")
    print("=" * 60)

    try:
        from src.core.nativecam_source import find_adb, adb_device_id, connect_nativecam

        # 1. adb 定位
        print("\n[1] 查找 adb.exe")
        adb = find_adb()
        print(f"    ADB: {adb or '未找到'}")
        if not adb:
            print("    请确认项目 ADB 目录完整，或设置 NATIVECAM_ADB 环境变量")
            return

        # 2. USB 设备检测
        print("\n[2] 检测 USB 手机")
        ok, msg = adb_device_id(adb)
        print(f"    状态: {'已连接' if ok else '未连接'}")
        print(f"    信息: {msg}")
        if not ok:
            return

        # 3. 全自动连接
        print("\n[3] 全自动连接（forward → 安装/授权 → 启动服务）")
        ok, cfg, msg = connect_nativecam()
        print(f"    结果: {'成功' if ok else '失败'}")
        print(f"    消息: {msg}")
        if not ok:
            return
        print(f"    分辨率: {cfg['width']}x{cfg['height']}")

        # 4. 帧捕获测试
        if capture:
            print("\n[4] 帧捕获测试")
            from src.core.media_source import create_video_source
            src = create_video_source(cfg)
            if not src.start():
                print("    [ERROR] 无法启动视频源")
                return
            print(f"    拉取 {frames} 帧...")
            got = 0
            for _ in range(frames):
                o, f, _ = src.read_frame()
                if o and f is not None:
                    got += 1
                    h, w = f.shape[:2]
                    print(f"    帧 {got}: {w}x{h}")
                else:
                    print(f"    帧 {got+1}: [FAILED]")
            src.stop()
            print(f"    成功 {got}/{frames} 帧")
    except Exception as e:
        print(f"\n[ERROR] {e}")


def test_adb():
    """测试 ADB 连接（项目内置 adb）。"""
    print("=" * 60)
    print("ADB 连接测试")
    print("=" * 60)

    try:
        from src.core.nativecam_source import find_adb, adb_device_id
        adb = find_adb()
        print(f"\nADB 路径: {adb or '未找到'}")
        if not adb:
            print("    请确认项目 ADB 目录完整，或设置 NATIVECAM_ADB 环境变量")
            return

        print(f"\n检测设备...")
        ok, msg = adb_device_id(adb)
        print(f"状态: {'已连接' if ok else '未连接'}")
        print(f"信息: {msg}")
    except Exception as e:
        print(f"\n[ERROR] {e}")


if __name__ == "__main__":
    main()
