@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where x86_64-w64-mingw32-gcc >nul 2>nul
if errorlevel 1 (
    echo 请安装 64 位 MinGW-w64 并将 bin 目录加入 PATH。
    pause
    exit /b 1
)
x86_64-w64-mingw32-windres launcher.rc -O coff -o launcher-res.o
if errorlevel 1 exit /b 1
x86_64-w64-mingw32-gcc -municode -mwindows -Os -Wall -Wextra -static -Wl,--nxcompat,--dynamicbase -o "..\帕琪工坊启动器.exe" launcher.c launcher-res.o -luser32
if errorlevel 1 exit /b 1
del launcher-res.o
echo 已生成帕琪工坊启动器.exe。
pause
