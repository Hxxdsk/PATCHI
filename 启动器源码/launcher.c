#ifndef UNICODE
#define UNICODE
#endif
#define _UNICODE
#include <windows.h>
#include <wchar.h>
#include <stdio.h>

static int error(const wchar_t *message) {
    MessageBoxW(NULL, message, L"帕琪工坊启动器", MB_OK | MB_ICONERROR);
    return 1;
}
int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR args, int show) {
    (void)previous; (void)args; (void)show;
    wchar_t dir[32768], runtime[32768], script[32768], powershell[32768], command[32768];
    DWORD length = GetModuleFileNameW(NULL, dir, 32768);
    if (!length || length >= 32000) return error(L"程序路径过长，请解压到较短的路径。");
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (!slash) return error(L"无法获取程序目录。");
    *slash = 0;
    if (swprintf(runtime,32768,L"%ls\\.runtime",dir)<0 ||
        swprintf(script,32768,L"%ls\\launcher.ps1",runtime)<0) return error(L"程序路径过长。");
    if (!CreateDirectoryW(runtime, NULL) && GetLastError()!=ERROR_ALREADY_EXISTS)
        return error(L"程序目录无法写入。请完整解压到文档或桌面下的文件夹，勿从压缩包内直接运行。");
    HRSRC res=FindResourceW(instance,MAKEINTRESOURCEW(101),RT_RCDATA);
    HGLOBAL data=res?LoadResource(instance,res):NULL;
    const void *bytes=data?LockResource(data):NULL;
    DWORD size=res?SizeofResource(instance,res):0, written=0;
    if (!bytes || !size) return error(L"启动器资源损坏，请重新解压。");
    HANDLE file=CreateFileW(script,GENERIC_WRITE,0,NULL,CREATE_ALWAYS,FILE_ATTRIBUTE_NORMAL,NULL);
    if (file==INVALID_HANDLE_VALUE) return error(L"无法写入启动器脚本，请检查目录权限或关闭已有启动器。");
    BOOL ok=WriteFile(file,bytes,size,&written,NULL);
    CloseHandle(file);
    if (!ok || written!=size) return error(L"写入启动器脚本失败。");
    wchar_t system[32768];
    if (!GetSystemDirectoryW(system,32768)) return error(L"无法获取 Windows 系统目录。");
    if (swprintf(powershell,32768,L"%ls\\WindowsPowerShell\\v1.0\\powershell.exe",system)<0 ||
        swprintf(command,32768,L"\"%ls\" -NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File \"%ls\" -AppDir \"%ls\"",powershell,script,dir)<0)
        return error(L"程序路径过长。");
    STARTUPINFOW si={0}; PROCESS_INFORMATION pi={0}; si.cb=sizeof(si);
    if (!CreateProcessW(powershell,command,NULL,NULL,FALSE,CREATE_NO_WINDOW,NULL,dir,&si,&pi))
        return error(L"无法启动 Windows PowerShell 5.1。需要 Windows 10／11 64 位及系统自带的 PowerShell、.NET Framework；受管理电脑请联系管理员。");
    CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
    return 0;
}
