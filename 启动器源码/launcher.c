#ifndef UNICODE
#define UNICODE
#endif
#define _UNICODE
#include <windows.h>
#include <wchar.h>
#include <wctype.h>
#include <stdio.h>

static int error(const wchar_t *message) {
    MessageBoxW(NULL,message,L"帕琪工坊启动器",MB_OK|MB_ICONERROR);
    return 1;
}
static int exists(const wchar_t *path) {
    DWORD attr=GetFileAttributesW(path);
    return attr!=INVALID_FILE_ATTRIBUTES && !(attr&FILE_ATTRIBUTE_DIRECTORY);
}
static void read_diagnostic(const wchar_t *path,wchar_t *output,int capacity) {
    output[0]=0;
    HANDLE file=CreateFileW(path,GENERIC_READ,FILE_SHARE_READ|FILE_SHARE_WRITE,NULL,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,NULL);
    if(file==INVALID_HANDLE_VALUE)return;
    char bytes[6000];DWORD got=0;
    BOOL ok=ReadFile(file,bytes,sizeof(bytes)-1,&got,NULL);CloseHandle(file);
    if(!ok||!got)return;
    bytes[got]=0;
    int skip=(got>=3 && (unsigned char)bytes[0]==0xef && (unsigned char)bytes[1]==0xbb && (unsigned char)bytes[2]==0xbf)?3:0;
    if(got>=2 && (unsigned char)bytes[0]==0xff && (unsigned char)bytes[1]==0xfe) {
        int count=(int)(got-2)/2;if(count>=capacity)count=capacity-1;
        for(int i=0;i<count;i++)output[i]=(wchar_t)((unsigned char)bytes[2+i*2]|((unsigned char)bytes[3+i*2]<<8));
        output[count]=0;return;
    }
    int n=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,bytes+skip,(int)got-skip,output,capacity-1);
    if(!n)n=MultiByteToWideChar(CP_ACP,0,bytes+skip,(int)got-skip,output,capacity-1);
    if(n>0)output[n]=0;
}
int WINAPI wWinMain(HINSTANCE instance,HINSTANCE previous,PWSTR args,int show) {
    (void)previous;(void)args;(void)show;
    wchar_t dir[32768],runtime[32768],script[32768],powershell[32768],command[32768];
    wchar_t log[32768],failure[32768],ready[32768];
    DWORD length=GetModuleFileNameW(NULL,dir,32768);
    if(!length||length>=10000)return error(L"程序路径过长，请完整解压到较短的普通文件夹。");
    wchar_t *slash=wcsrchr(dir,L'\\');if(!slash)return error(L"无法获取程序目录。");*slash=0;
    if(swprintf(runtime,32768,L"%ls\\.runtime",dir)<0||swprintf(script,32768,L"%ls\\launcher.ps1",runtime)<0||
       swprintf(log,32768,L"%ls\\launcher.bootstrap.log",runtime)<0||swprintf(failure,32768,L"%ls\\launcher.failure.txt",runtime)<0||
       swprintf(ready,32768,L"%ls\\launcher.ready",runtime)<0)return error(L"程序路径过长。");
    if(!CreateDirectoryW(runtime,NULL)&&GetLastError()!=ERROR_ALREADY_EXISTS)
        return error(L"程序目录无法写入。请完整解压到文档或桌面下的文件夹，勿从压缩包内直接运行。");
    // Serialize resource extraction and readiness polling during cold startup.
    unsigned long long hash=14695981039346656037ULL;
    for(const wchar_t *p=dir;*p;p++){hash^=(unsigned long long)towlower(*p);hash*=1099511628211ULL;}
    wchar_t mutexName[100];swprintf(mutexName,100,L"Local\\PatchiBootstrap_%016llx",hash);
    HANDLE mutex=CreateMutexW(NULL,TRUE,mutexName);
    if(!mutex)return error(L"无法创建启动器，请重新运行。");
    if(GetLastError()==ERROR_ALREADY_EXISTS){CloseHandle(mutex);MessageBoxW(NULL,L"启动器正在准备窗口，请稍候。",L"帕琪工坊",MB_OK|MB_ICONINFORMATION);return 0;}
    DeleteFileW(ready);DeleteFileW(failure);
    HRSRC res=FindResourceW(instance,MAKEINTRESOURCEW(101),RT_RCDATA);
    HGLOBAL data=res?LoadResource(instance,res):NULL;const void *bytes=data?LockResource(data):NULL;
    DWORD size=res?SizeofResource(instance,res):0,written=0;
    int result=1;
    if(!bytes||!size){error(L"启动器资源损坏，请重新解压。");goto finish;}
    HANDLE file=CreateFileW(script,GENERIC_WRITE,0,NULL,CREATE_ALWAYS,FILE_ATTRIBUTE_NORMAL,NULL);
    if(file==INVALID_HANDLE_VALUE){error(L"无法写入启动器脚本，请检查目录权限或关闭已有启动器。");goto finish;}
    BOOL ok=WriteFile(file,bytes,size,&written,NULL);CloseHandle(file);
    if(!ok||written!=size){error(L"写入启动器脚本失败。");goto finish;}
    wchar_t system[32768];
    if(!GetSystemDirectoryW(system,32768)){error(L"无法获取 Windows 系统目录。");goto finish;}
    if(swprintf(powershell,32768,L"%ls\\WindowsPowerShell\\v1.0\\powershell.exe",system)<0||
       swprintf(command,32768,L"\"%ls\" -NoLogo -NoProfile -NonInteractive -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File \"%ls\" -AppDir \"%ls\"",powershell,script,dir)<0){error(L"程序路径过长。");goto finish;}
    SECURITY_ATTRIBUTES sa={sizeof(sa),NULL,TRUE};
    HANDLE output=CreateFileW(log,GENERIC_WRITE,FILE_SHARE_READ|FILE_SHARE_WRITE,&sa,CREATE_ALWAYS,FILE_ATTRIBUTE_NORMAL,NULL);
    HANDLE input=CreateFileW(L"NUL",GENERIC_READ,FILE_SHARE_READ|FILE_SHARE_WRITE,&sa,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,NULL);
    if(output==INVALID_HANDLE_VALUE||input==INVALID_HANDLE_VALUE){
        if(output!=INVALID_HANDLE_VALUE)CloseHandle(output);
        if(input!=INVALID_HANDLE_VALUE)CloseHandle(input);
        error(L"无法创建启动日志，请检查程序目录写入权限。");goto finish;
    }
    STARTUPINFOW si={0};PROCESS_INFORMATION pi={0};si.cb=sizeof(si);si.dwFlags=STARTF_USESTDHANDLES;
    si.hStdOutput=output;si.hStdError=output;si.hStdInput=input;
    BOOL started=CreateProcessW(powershell,command,NULL,NULL,TRUE,CREATE_NO_WINDOW,NULL,dir,&si,&pi);
    DWORD createError=GetLastError();CloseHandle(output);CloseHandle(input);
    if(!started){
        wchar_t detail[600];swprintf(detail,600,L"无法启动系统 Windows PowerShell（错误 %lu）。需要 Windows 10／11 64 位的 PowerShell 5.1 和 .NET Framework。",createError);
        error(detail);goto finish;
    }
    CloseHandle(pi.hThread);
    // A parse/assembly failure before the form shows must never disappear silently.
    DWORD tick=GetTickCount();result=0;
    for(;;){
        DWORD state=WaitForSingleObject(pi.hProcess,50);
        if(state==WAIT_OBJECT_0){
            DWORD exitCode=0;GetExitCodeProcess(pi.hProcess,&exitCode);
            if(exitCode!=0||exists(failure)){
                if(exists(ready)){result=1;break;}
                wchar_t reason[6100],message[32768];read_diagnostic(exists(failure)?failure:log,reason,6100);
                swprintf(message,32768,L"启动器未能打开（退出码 %lu）。\n\n%ls\n\n启动记录：%ls\n请完整解压最新版；可运行“诊断启动器.bat”查看过程。",exitCode,reason[0]?reason:L"请查看启动记录中的详细原因。",log);
                result=error(message);
            }
            break;
        }
        if(exists(ready))break;
        // Cold .NET initialization can be slow. Leave the child alive to finish.
        if((DWORD)(GetTickCount()-tick)>20000)break;
    }
    CloseHandle(pi.hProcess);
finish:
    ReleaseMutex(mutex);CloseHandle(mutex);return result;
}
