@echo off
chcp 65001 >nul
REM ============================================================
REM  NativeCam 编译脚本 (纯命令行, 无需 Android Studio / Gradle)
REM  产出: build\nativecam.apk
REM ============================================================
setlocal

set JDK_HOME=%~dp0..\jdk\jdk-17.0.20.1+1
set PATH=%JDK_HOME%\bin;%PATH%
set SDK=%~dp0..\android-sdk
set BT=%SDK%\build-tools\34.0.0
set ANDROID_JAR=%SDK%\platforms\android-30\android.jar
set PROJ=%~dp0
set BUILD=%PROJ%build

echo ============================================================
echo  NativeCam build
echo ============================================================

if not exist "%ANDROID_JAR%" (
    echo [X] 缺少 android.jar: %ANDROID_JAR%
    echo     请先运行 sdkmanager 安装 platforms;android-30
    goto :fail
)
if not exist "%BT%\aapt2.exe" (
    echo [X] 缺少 build-tools: %BT%
    goto :fail
)

if not exist "%BUILD%" mkdir "%BUILD%"

REM ---- 1. 生成签名 keystore (首次) ----
if not exist "%BUILD%\nativecam.keystore" (
    echo [1/6] 生成 keystore ...
    "%JDK_HOME%\bin\keytool" -genkeypair -keystore "%BUILD%\nativecam.keystore" ^
        -alias nativecam -keyalg RSA -keysize 2048 -validity 10950 ^
        -storepass nativecam -keypass nativecam ^
        -dname "CN=NativeCam, OU=Dev, O=Dev, C=CN" 2>nul
) else (
    echo [1/6] keystore 已存在, 跳过
)

REM ---- 2. aapt2 link (本项目无 res, 只需打包 manifest) ----
echo [2/6] aapt2 link ...
"%BT%\aapt2" link -I "%ANDROID_JAR%" --manifest "%PROJ%AndroidManifest.xml" ^
    --min-sdk-version 23 --target-sdk-version 30 ^
    -o "%BUILD%\unsigned.apk" --auto-add-overlay
if errorlevel 1 (
    echo [X] aapt2 link 失败
    goto :fail
)

REM ---- 3. javac 编译 (显式列出源文件: cmd 不展开引号内通配符) ----
echo [3/6] javac ...
if not exist "%BUILD%\classes" mkdir "%BUILD%\classes"
"%JDK_HOME%\bin\javac" -nowarn -encoding UTF-8 ^
    -classpath "%ANDROID_JAR%" ^
    -d "%BUILD%\classes" ^
    "%PROJ%src\com\nativecam\bridge\MainActivity.java" ^
    "%PROJ%src\com\nativecam\bridge\CameraService.java"
if errorlevel 1 (
    echo [X] javac 失败
    goto :fail
)

REM ---- 4. 先打包 class 为 jar 再 d8 (d8 不接受目录作为输入) ----
echo [4/6] jar + d8 ...
if exist "%BUILD%\classes.jar" del "%BUILD%\classes.jar"
"%JDK_HOME%\bin\jar" cf "%BUILD%\classes.jar" -C "%BUILD%\classes" com
if errorlevel 1 (
    echo [X] jar 打包失败
    goto :fail
)
"%JDK_HOME%\bin\java" -cp "%BT%\lib\d8.jar" com.android.tools.r8.D8 --lib "%ANDROID_JAR%" --min-api 23 --output "%BUILD%" ^
    "%BUILD%\classes.jar"
if errorlevel 1 (
    echo [X] d8 失败
    goto :fail
)
if not exist "%BUILD%\classes.dex" (
    echo [X] 未生成 classes.dex
    goto :fail
)

REM ---- 5. 把 dex 塞进 apk ----
echo [5/6] 合并 dex 到 apk ...
"%JDK_HOME%\bin\jar" uf "%BUILD%\unsigned.apk" -C "%BUILD%" classes.dex
if errorlevel 1 (
    echo [X] 合并 dex 失败
    goto :fail
)

REM ---- 6. 对齐 + 签名 ----
echo [6/6] zipalign + apksigner ...
"%BT%\zipalign" -f 4 "%BUILD%\unsigned.apk" "%BUILD%\aligned.apk"
if errorlevel 1 (
    echo [X] zipalign 失败
    goto :fail
)
"%JDK_HOME%\bin\java" -jar "%BT%\lib\apksigner.jar" sign --ks "%BUILD%\nativecam.keystore" ^
    --ks-key-alias nativecam --ks-pass pass:nativecam --key-pass pass:nativecam ^
    --out "%BUILD%\nativecam.apk" "%BUILD%\aligned.apk"
if errorlevel 1 (
    echo [X] apksigner 失败
    goto :fail
)

echo.
echo [OK] 构建成功: %BUILD%\nativecam.apk
dir "%BUILD%\nativecam.apk"
echo.
echo 安装到手机:
echo   adb install -r %BUILD%\nativecam.apk
echo   adb shell pm grant com.nativecam.bridge android.permission.CAMERA
echo   adb shell am start-foreground-service -n com.nativecam.bridge/.CameraService
echo   adb forward tcp:8888 tcp:8888
echo   python pc\nativecam.py info
goto :eof

:fail
echo.
echo [X] 构建失败
exit /b 1
