# CourseRecorder launcher (PowerShell)
# Run with:  powershell -ExecutionPolicy Bypass -File run.ps1

$ErrorActionPreference = "Continue"

$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $root

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " CourseRecorder" -ForegroundColor Cyan
Write-Host " Folder: $root" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# --- Pick a Python interpreter ------------------------------------------------
$candidates = @(
    "D:\Data\Anaconda3\python.exe",
    (Join-Path $env:USERPROFILE "anaconda3\python.exe"),
    (Join-Path $env:USERPROFILE "miniconda3\python.exe"),
    "C:\ProgramData\Anaconda3\python.exe"
)

$pyexe = $null
foreach ($c in $candidates) {
    if ($c -and (Test-Path $c)) { $pyexe = $c; break }
}
if (-not $pyexe) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $pyexe = $cmd.Source }
}

if (-not $pyexe) {
    Write-Host "[ERROR] Python not found." -ForegroundColor Red
    Write-Host "Please install Anaconda, or edit run.ps1 and set the path manually."
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "Python: $pyexe"
& $pyexe --version

# --- Sanity check -------------------------------------------------------------
if (-not (Test-Path (Join-Path $root "main.py"))) {
    Write-Host "[ERROR] main.py not found in $root" -ForegroundColor Red
    Write-Host "Make sure you extracted the whole project folder."
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host ""
Write-Host "Starting CourseRecorder ..." -ForegroundColor Green
Write-Host ""

& $pyexe (Join-Path $root "main.py")
$rc = $LASTEXITCODE

Write-Host ""
if ($rc -ne 0) {
    Write-Host "[ERROR] CourseRecorder exited with code $rc" -ForegroundColor Red
} else {
    Write-Host "CourseRecorder closed normally." -ForegroundColor Green
}
Read-Host "Press Enter to exit"
