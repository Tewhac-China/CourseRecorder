@echo off
setlocal enabledelayedexpansion

REM ============================================================
REM  CourseRecorder launcher (ASCII only, no chcp)
REM ============================================================

REM Move to the folder that contains this .bat (handles paths with spaces)
pushd "%~dp0"
if errorlevel 1 (
    echo [ERROR] Cannot change to script folder: %~dp0
    pause
    exit /b 1
)

echo ============================================================
echo  CourseRecorder
echo  Folder: %CD%
echo ============================================================

REM --- Pick a Python interpreter ------------------------------------------------
set "PYEXE="

if not defined PYEXE if exist "%USERPROFILE%\anaconda3\python.exe" set "PYEXE=%USERPROFILE%\anaconda3\python.exe"
if not defined PYEXE if exist "%USERPROFILE%\miniconda3\python.exe" set "PYEXE=%USERPROFILE%\miniconda3\python.exe"
if not defined PYEXE if exist "C:\ProgramData\Anaconda3\python.exe" set "PYEXE=C:\ProgramData\Anaconda3\python.exe"

REM Fall back to whatever is on PATH
if not defined PYEXE (
    where python >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
)

if not defined PYEXE (
    echo.
    echo [ERROR] Python not found.
    echo Please install Anaconda, or edit run.bat and set PYEXE manually.
    echo.
    pause
    exit /b 1
)

echo Python: %PYEXE%
"%PYEXE%" --version

REM --- Sanity check ------------------------------------------------------------
if not exist "main.py" (
    echo.
    echo [ERROR] main.py not found in %CD%
    echo Make sure you extracted the whole project folder.
    echo.
    pause
    exit /b 1
)

echo.
echo Starting CourseRecorder ...
echo.

"%PYEXE%" main.py

set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo [ERROR] CourseRecorder exited with code %RC%
) else (
    echo CourseRecorder closed normally.
)
echo.
pause
popd
endlocal
