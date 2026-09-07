@echo off
chcp 65001 >nul
title NativeCam PC - 手机摄像头最高分辨率获取工具
cd /d "%~dp0"

:menu
cls
echo ============================================================
echo   NativeCam  手机摄像头最高分辨率获取 (USB)
echo   当前设备最高: 3264 x 2448  (Camera2 标准 API 极限)
echo ============================================================
echo.
echo   [1] 抓取一帧最高分辨率画面 -^> photo.jpg
echo   [2] 实时显示画面 (按 ESC 或 Q 退出)
echo   [3] 启动 HTTP 接口服务 (端口 8080, 供二次开发)
echo   [4] 查看相机状态与支持尺寸
echo   [5] 设置分辨率
echo   [6] 设置 JPEG 画质
echo   [7] 重启相机
echo   [8] 停止服务并退出 App
echo   [0] 退出
echo.
set /p choice=请选择并回车: 

if "%choice%"=="1" goto grab
if "%choice%"=="2" goto show
if "%choice%"=="3" goto serve
if "%choice%"=="4" goto info
if "%choice%"=="5" goto setres
if "%choice%"=="6" goto setq
if "%choice%"=="7" goto restart
if "%choice%"=="8" goto stop
if "%choice%"=="0" goto end
goto menu

:grab
cls
echo 正在抓取 (最高分辨率) ...
python nativecam.py grab -o "%~dp0photo.jpg"
echo.
pause
goto menu

:show
cls
echo 实时显示中 (数据为原生分辨率, 窗口为适配屏幕缩放) ... 按 ESC 或 Q 退出
python nativecam.py show
goto menu

:serve
cls
echo 启动本地 HTTP 接口服务 ...
echo   单帧:     http://127.0.0.1:8080/frame
echo   MJPEG流:  http://127.0.0.1:8080/stream
echo   状态:     http://127.0.0.1:8080/info
echo   Ctrl+C 停止
echo.
python nativecam.py serve --serve-port 8080
pause
goto menu

:info
cls
python nativecam.py info
echo.
echo --- 支持的 JPEG 尺寸 ---
python nativecam.py sizes
echo.
pause
goto menu

:setres
cls
echo 常用分辨率: 3264x2448 (最高)  1920x1080  1280x720  640x480
set /p rs=输入分辨率(如 1920x1080)或 auto: 
python nativecam.py setres --size "%rs%"
echo.
pause
goto menu

:setq
cls
set /p q=输入画质 1-100 (默认 95): 
python nativecam.py setquality --quality %q%
echo.
pause
goto menu

:restart
cls
echo 重启相机 ...
python nativecam.py restart
echo.
pause
goto menu

:stop
cls
echo 停止服务并退出 App ...
python nativecam.py stop
echo.
pause
goto menu

:end
exit
