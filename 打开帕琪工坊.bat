@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "帕琪工坊启动器.exe" (
    echo 更新包不完整，请完整解压后重新运行。
    pause
    exit /b 1
)
start "" "%~dp0帕琪工坊启动器.exe"
