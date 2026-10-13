@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "启动器源码\launcher.ps1" (
    echo 缺少启动器源码，请完整解压更新包。
    pause
    exit /b 1
)
if not exist ".runtime" mkdir ".runtime"
set "PATCHI_PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if exist "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "PATCHI_PS=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
"%PATCHI_PS%" -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0启动器源码\launcher.ps1" -AppDir "%~dp0."
set "PATCHI_CODE=%errorlevel%"
echo.
echo 启动过程已结束，退出码 %PATCHI_CODE%。
echo 错误记录位于本目录 .runtime 文件夹。
pause
exit /b %PATCHI_CODE%
