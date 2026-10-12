param([Parameter(Mandatory=$true)][string]$AppDir)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Net.Http
[System.Windows.Forms.Application]::EnableVisualStyles()
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$script:Runtime = Join-Path $AppDir '.runtime'
$script:Log = Join-Path $script:Runtime 'launcher.log'
$script:Busy = $false
$script:StopRequested = $false
$script:State = $null
$Version = '2026.10.12.3'
$PythonUrl = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe'
$PythonSha = '67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb'
$Probe = @'
import sys,json,struct,importlib,importlib.metadata as m
ok=(3,10)<=sys.version_info[:2]<(3,14) and struct.calcsize('P')==8
bad=[]
for mod,dist,lo,hi in [('numpy','numpy',(1,24),(3,)),('cv2','opencv-python',(4,8),(5,)),('PIL','Pillow',(10,),(13,)),('playwright.sync_api','playwright',(1,40),(2,))]:
 try:
  x=importlib.import_module(mod)
  if mod=='cv2' and not hasattr(x,'SIFT_create'): raise ValueError('SIFT')
  try: version=m.version(dist)
  except m.PackageNotFoundError:
   if dist!='opencv-python': raise
   version=m.version('opencv-python-headless')
  ver=tuple(int(n) for n in version.split('.')[:2])
  if not lo<=ver<hi: raise ValueError('version')
 except Exception: bad.append(dist)
print(json.dumps({'supported':ok,'exe':sys.executable,'version':sys.version.split()[0],'missing':bad},ensure_ascii=True))
'@
function Quote-Arg([string]$Value) {
    if ($Value.Contains('"')) { throw '命令参数含无效双引号' }
    return '"' + ($Value -replace '(\\+)$', '$1$1') + '"'
}
function Pump {
    [System.Windows.Forms.Application]::DoEvents()
    if ($script:StopRequested) { throw '操作已取消，可以重新检查后重试。' }
}
function Write-Log([string]$Text) {
    $line = '[' + (Get-Date -Format 'HH:mm:ss') + '] ' + $Text
    $script:Details.AppendText($line + [Environment]::NewLine)
    Add-Content -LiteralPath $script:Log -Value $line -Encoding UTF8
}
function Set-Status([string]$Text) { $script:Status.Text = $Text; Pump }
function Invoke-Child([string]$Exe, [string[]]$ArgsList, [int]$TimeoutSec=600) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName=$Exe; $psi.Arguments=($ArgsList | ForEach-Object { Quote-Arg $_ }) -join ' '
    $psi.WorkingDirectory=$AppDir; $psi.UseShellExecute=$false; $psi.CreateNoWindow=$true
    $psi.RedirectStandardOutput=$true; $psi.RedirectStandardError=$true
    $psi.StandardOutputEncoding=[Text.Encoding]::UTF8; $psi.StandardErrorEncoding=[Text.Encoding]::UTF8
    $psi.EnvironmentVariables['PYTHONUTF8']='1'
    $psi.EnvironmentVariables.Remove('PYTHONHOME'); $psi.EnvironmentVariables.Remove('PYTHONPATH')
    $p=New-Object System.Diagnostics.Process; $p.StartInfo=$psi
    $out=$null; $err=$null
    try {
        if (-not $p.Start()) { throw '无法启动组件进程' }
        $out=$p.StandardOutput.ReadToEndAsync(); $err=$p.StandardError.ReadToEndAsync()
        $watch=[Diagnostics.Stopwatch]::StartNew()
        while (-not $p.HasExited) {
            Pump
            if ($watch.Elapsed.TotalSeconds -gt $TimeoutSec) { throw '组件检查或安装超时，请查看日志并重试。' }
            [Threading.Thread]::Sleep(60)
        }
        $p.WaitForExit()
        return [pscustomobject]@{Code=$p.ExitCode; Out=$out.Result; Err=$err.Result}
    } finally {
        if ($p.Id -and -not $p.HasExited) {
            # Cancel only the process started by this operation and its children.
            $killer=Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\taskkill.exe') -ArgumentList @('/PID', [string]$p.Id, '/T', '/F') -WindowStyle Hidden -PassThru
            $null=$killer.WaitForExit(3000)
        }
        $p.Dispose()
    }
}
function Probe-Python([string]$Exe) {
    try {
        if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) { return $null }
        if ($Exe -like '*\WindowsApps\*') { return $null }
        $r=Invoke-Child $Exe @('-c',$Probe) 20
        if ($r.Code -ne 0) { return $null }
        $x=$r.Out.Trim() | ConvertFrom-Json
        if ($x.supported) { return $x }
    } catch { if($script:StopRequested){throw}; Write-Log ('跳过不可用 Python：'+$Exe) }
    return $null
}
function Find-Python {
    $paths=New-Object System.Collections.Generic.List[string]
    $paths.Add((Join-Path $AppDir '.venv\Scripts\python.exe'))
    $paths.Add((Join-Path $script:Runtime 'python\python.exe'))
    foreach($root in @('HKCU:\Software\Python\PythonCore','HKLM:\Software\Python\PythonCore','HKLM:\Software\WOW6432Node\Python\PythonCore')) {
        if(Test-Path $root) { foreach($key in Get-ChildItem $root -ErrorAction SilentlyContinue) {
            $ip=Join-Path $key.PSPath 'InstallPath'
            if(Test-Path $ip) {
                $props=Get-ItemProperty $ip
                if($props.ExecutablePath){$paths.Add([string]$props.ExecutablePath)}
                $v=(Get-Item $ip).GetValue(''); if($v){$paths.Add((Join-Path $v 'python.exe'))}
            }
        }}
    }
    foreach($cmd in @(Get-Command python.exe,python3.exe -ErrorAction SilentlyContinue)) { if($cmd){$paths.Add($cmd.Source)} }
    foreach($p in @(Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python*\python.exe') -ErrorAction SilentlyContinue)){$paths.Add($p.FullName)}
    $py=Get-Command py.exe -ErrorAction SilentlyContinue
    if($py) {
        try {
            $r=Invoke-Child $py.Source @('-0p') 15
            foreach($line in ($r.Out -split "`n")) { if($line -match '(?<path>[A-Za-z]:\\.+?python(?:\d+)?\.exe)\s*$'){$paths.Add($Matches.path.Trim())} }
        } catch {if($script:StopRequested){throw}}
    }
    $fallback=$null
    foreach($path in @($paths | Select-Object -Unique)) {
        $result=Probe-Python $path
        if($result) { if(@($result.missing).Count -eq 0){return $result}; if(-not $fallback){$fallback=$result} }
    }
    return $fallback
}
function Find-Browser {
    $paths=New-Object System.Collections.Generic.List[string]
    foreach($base in @(${env:ProgramFiles},${env:ProgramFiles(x86)},$env:LOCALAPPDATA)) {
        if($base) {foreach($suffix in @('Microsoft\Edge\Application\msedge.exe','Google\Chrome\Application\chrome.exe')){$paths.Add((Join-Path $base $suffix))}}
    }
    foreach($root in @('HKCU:\Software\Microsoft\Windows\CurrentVersion\App Paths','HKLM:\Software\Microsoft\Windows\CurrentVersion\App Paths')) {
        foreach($exe in @('msedge.exe','chrome.exe')) {
            $key=Join-Path $root $exe
            if(Test-Path $key){$v=(Get-Item $key).GetValue('');if($v){$paths.Add([string]$v)}}
        }
    }
    foreach($name in @('msedge.exe','chrome.exe')){$cmd=Get-Command $name -ErrorAction SilentlyContinue;if($cmd){$paths.Add($cmd.Source)}}
    foreach($path in @($paths | Select-Object -Unique)){
        if(Test-Path -LiteralPath $path -PathType Leaf){
            $ver=[Diagnostics.FileVersionInfo]::GetVersionInfo($path)
            if($ver.FileMajorPart -ge 120){return $path}
        }
    }
    return $null
}
function Check-Environment {
    Set-Status '正在检查程序文件、Python、依赖和浏览器…'
    foreach($name in @('帕琪工坊.py','帕琪工坊_智能详情页制作工具.html','patchi_samephoto.py','patchi_download_stable.py','requirements_套图.txt')) {
        if(-not(Test-Path -LiteralPath (Join-Path $AppDir $name) -PathType Leaf)){throw ('更新包缺少 '+$name+'，请完整解压后启动。')}
    }
    $python=Find-Python; $browser=Find-Browser
    $script:State=[pscustomobject]@{Python=$python;Browser=$browser}
    $items=@()
    if($python){$items+='Python '+$python.version+'（64 位）';if(@($python.missing).Count){$items+='缺少或版本不兼容：'+(@($python.missing)-join '、')}}
    else{$items+='缺少可用的 64 位 Python 3.10～3.13（将下载 Python 3.12.10）'}
    if($browser){$items+='浏览器：'+[IO.Path]::GetFileName($browser)}else{$items+='缺少受支持浏览器：请自行安装桌面版 Edge 或 Chrome 120+，建议最新版。启动器不下载浏览器。'}
    Write-Log ($items -join [Environment]::NewLine)
    $missing=(-not $python) -or @($python.missing).Count -gt 0
    $script:Install.Enabled=$missing
    $script:Launch.Enabled=(-not $missing) -and [bool]$browser
    if($missing){Set-Status '检测到缺少组件。点击“下载并安装缺少组件”后会直接下载并安装。'}
    elseif(-not $browser){Set-Status 'Python 和依赖已就绪；请安装新版 Edge／Chrome 后重新检查。'}
    else{Set-Status '环境已就绪，正在启动帕琪工坊…';Start-Workshop}
}
function Download-Python {
    $dir=Join-Path $script:Runtime 'downloads'; $null=[IO.Directory]::CreateDirectory($dir)
    $dest=Join-Path $dir 'python-3.12.10-amd64.exe'; $part=$dest+'.part'
    if((Test-Path $dest) -and ((Get-FileHash $dest -Algorithm SHA256).Hash.ToLowerInvariant() -eq $PythonSha)){return $dest}
    $client=New-Object Net.Http.HttpClient; $client.Timeout=[TimeSpan]::FromMinutes(15)
    $response=$null; $stream=$null; $file=$null
    try {
        Set-Status '正在从 python.org 直接下载 Python 安装器…'
        Write-Log ('下载地址：'+$PythonUrl)
        $task=$client.GetAsync($PythonUrl,[Net.Http.HttpCompletionOption]::ResponseHeadersRead)
        while(-not $task.IsCompleted){Pump;[Threading.Thread]::Sleep(60)}
        $response=$task.GetAwaiter().GetResult();$null=$response.EnsureSuccessStatusCode()
        $stream=$response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $file=[IO.File]::Open($part,[IO.FileMode]::Create)
        $buffer=New-Object byte[] 65536; $total=[long]0;$length=$response.Content.Headers.ContentLength
        $script:Progress.Style='Continuous'
        while($true){
            $read=$stream.ReadAsync($buffer,0,$buffer.Length)
            while(-not $read.IsCompleted){Pump;[Threading.Thread]::Sleep(40)}
            $n=$read.GetAwaiter().GetResult();if($n -eq 0){break}
            $file.Write($buffer,0,$n);$total+=$n
            if($length){$script:Progress.Value=[int][Math]::Min(100,100*$total/$length)}
            Set-Status ('正在下载 Python：{0:N1} MB / {1:N1} MB' -f ($total/1MB),($length/1MB))
        }
        $file.Dispose();$file=$null
        if($length -and $total -ne $length){throw '下载不完整，请重试。'}
        if((Get-FileHash $part -Algorithm SHA256).Hash.ToLowerInvariant() -ne $PythonSha){throw 'Python 安装器 SHA256 校验失败，请重新下载。'}
        $signature=Get-AuthenticodeSignature -LiteralPath $part
        if($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation'){throw 'Python 安装器数字签名校验失败，未运行安装器。'}
        Move-Item -LiteralPath $part -Destination $dest -Force
        Write-Log 'Python 安装器 SHA256 和官方数字签名校验通过。'
        return $dest
    } finally {
        if($file){$file.Dispose()};if($stream){$stream.Dispose()};if($response){$response.Dispose()};$client.Dispose()
        if(Test-Path $part){Remove-Item -LiteralPath $part -Force}
        $script:Progress.Style='Marquee'
    }
}
function Install-Components {
    if(-not $script:State){throw '请先检查环境'}
    $python=$script:State.Python
    if(-not $python){
        $installer=Download-Python
        Set-Status '正在为当前用户安装 Python，请稍候…'
        $target=Join-Path $script:Runtime 'python'
        $r=Invoke-Child $installer @('/quiet','InstallAllUsers=0',('TargetDir='+$target),'Include_launcher=0','Include_test=0','Include_doc=0','Include_tcltk=0','PrependPath=0','Shortcuts=0') 600
        if($r.Code -notin @(0,3010)){throw ('Python 安装失败，退出码 '+$r.Code+'。可以手动运行 .runtime\downloads 下的安装器。')}
        $python=Probe-Python (Join-Path $target 'python.exe')
        if(-not $python){throw 'Python 安装后未能运行，请检查安装日志或重新安装。'}
    }
    # Keep existing system installations untouched: dependencies go into this app's venv.
    $venv=Join-Path $AppDir '.venv';$venvExe=Join-Path $venv 'Scripts\python.exe'
    $local=Probe-Python $venvExe
    if(-not $local){
        if(Test-Path $venv){Move-Item -LiteralPath $venv -Destination ($venv+'.backup-'+(Get-Date -Format 'yyyyMMddHHmmss'))}
        Set-Status '正在创建程序专用 Python 环境…'
        $r=Invoke-Child $python.exe @('-m','venv',$venv) 120
        if($r.Code -ne 0){Write-Log($r.Out+$r.Err);throw '创建专用环境失败，请确认目录可写或重新安装 Python。'}
    }
    Set-Status '正在检查专用环境的 pip…'
    $r=Invoke-Child $venvExe @('-m','ensurepip','--upgrade') 120
    if($r.Code -ne 0){Write-Log($r.Out+$r.Err);throw '专用环境的 pip 无法修复，请重新检查或重新安装 Python。'}
    Set-Status '正在下载并安装 numpy、OpenCV、Pillow 和 Playwright（不下载浏览器）…'
    $r=Invoke-Child $venvExe @('-m','pip','install','--disable-pip-version-check','--no-input','--only-binary=:all:','-r',(Join-Path $AppDir 'requirements_套图.txt')) 900
    Write-Log($r.Out+$r.Err)
    if($r.Code -ne 0){throw '依赖下载或安装失败。请检查网络，点击重新检查后重试；详情见 launcher.log。'}
    Check-Environment
}
function Get-Health {
    $req=[Net.HttpWebRequest]::Create('http://127.0.0.1:8765/health')
    $req.Proxy=$null;$req.Timeout=500;$req.ReadWriteTimeout=500
    $res=$null;$reader=$null
    try{$res=$req.GetResponse();$reader=New-Object IO.StreamReader($res.GetResponseStream());return ($reader.ReadToEnd()|ConvertFrom-Json)}
    catch{return $null}
    finally{if($reader){$reader.Dispose()};if($res){$res.Dispose()}}
}
function Start-Workshop {
    if(-not $script:State.Browser){throw '请先安装桌面版 Edge 或 Chrome 120+。'}
    $health=Get-Health
    if($health -and ($health.service -ne 'patchi-helper' -or $health.appVersion -ne $Version)){throw '8765 端口正在运行旧版或其他服务，请关闭旧助手后重试。'}
    $helper=$null
    if(-not $health){
        Set-Status '正在启动本地助手…'
        $env:PAQI_NO_BROWSER='1';$env:PAQI_BROWSER_BIN=$script:State.Browser;$env:PYTHONUTF8='1'
        Remove-Item Env:PYTHONHOME,Env:PYTHONPATH -ErrorAction SilentlyContinue
        $scriptFile=Join-Path $AppDir '帕琪工坊.py'
        $helper=Start-Process -FilePath $script:State.Python.exe -ArgumentList @('-u',(Quote-Arg $scriptFile)) -WorkingDirectory $AppDir -WindowStyle Hidden -RedirectStandardOutput (Join-Path $script:Runtime 'helper.out.log') -RedirectStandardError (Join-Path $script:Runtime 'helper.err.log') -PassThru
        $watch=[Diagnostics.Stopwatch]::StartNew()
        while($watch.Elapsed.TotalSeconds -lt 25){
            Pump;$health=Get-Health
            if($health){break}
            if($helper.HasExited){throw '助手启动失败，请查看 .runtime\helper.err.log。'}
            [Threading.Thread]::Sleep(120)
        }
        if(-not $health){if(-not $helper.HasExited){$helper.Kill()};throw '助手未在 25 秒内就绪，请查看 .runtime 下的日志。'}
        if($health.service -ne 'patchi-helper' -or $health.appVersion -ne $Version){if(-not $helper.HasExited){$helper.Kill()};throw '8765 端口被其他服务占用，请关闭旧助手。'}
    }
    $null=Start-Process -FilePath $script:State.Browser -ArgumentList 'http://127.0.0.1:8765/'
    Write-Log '已启动脚本并打开网页。助手在后台运行；重复启动会复用本版助手。'
    $script:Form.Tag='success';$script:Form.Close()
}
function Run-Action([scriptblock]$Action) {
    if($script:Busy){return}
    $script:Busy=$true;$script:StopRequested=$false
    $script:Check.Enabled=$false;$script:Install.Enabled=$false;$script:Launch.Enabled=$false
    $script:Progress.Style='Marquee'
    try{& $Action}
    catch{$script:Status.Text=$_.Exception.Message;Write-Log ('失败：'+$_.Exception.Message)}
    finally{
        $script:Busy=$false;$script:StopRequested=$false;$script:Check.Enabled=$true;$script:Progress.Style='Continuous';$script:Progress.Value=0
        if($script:State){$missing=(-not $script:State.Python) -or @($script:State.Python.missing).Count -gt 0;$script:Install.Enabled=$missing;$script:Launch.Enabled=(-not $missing)-and[bool]$script:State.Browser}
        if($script:Form.Tag -eq 'close'){$script:Form.Close()}
    }
}
try {
    $null=[IO.Directory]::CreateDirectory($script:Runtime)
    $hash=[Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($AppDir.ToLowerInvariant()))
    $mutex=New-Object Threading.Mutex($false,('Local\PatchiLauncher_'+[BitConverter]::ToString($hash).Replace('-','')))
    if(-not $mutex.WaitOne(0)) { [Windows.Forms.MessageBox]::Show('此目录的启动器已打开，请查看已有窗口。','帕琪工坊')|Out-Null;exit }
    $script:Form=New-Object Windows.Forms.Form
    $script:Form.Text='帕琪工坊 · 环境检查与启动'
    $script:Form.ClientSize=New-Object Drawing.Size(760,520)
    $script:Form.StartPosition='CenterScreen';$script:Form.MinimumSize=$script:Form.Size
    $script:Form.Font=New-Object Drawing.Font('Microsoft YaHei UI',10)
    $title=New-Object Windows.Forms.Label;$title.Text='帕琪工坊';$title.Font=New-Object Drawing.Font('Microsoft YaHei UI',20,[Drawing.FontStyle]::Bold);$title.SetBounds(24,18,710,45)
    $script:Status=New-Object Windows.Forms.Label;$script:Status.Text='准备检查运行环境…';$script:Status.SetBounds(24,75,710,68)
    $script:Progress=New-Object Windows.Forms.ProgressBar;$script:Progress.SetBounds(24,147,710,14)
    $script:Details=New-Object Windows.Forms.TextBox;$script:Details.Multiline=$true;$script:Details.ReadOnly=$true;$script:Details.ScrollBars='Vertical';$script:Details.SetBounds(24,177,710,210);$script:Details.Anchor='Top,Bottom,Left,Right'
    $script:Check=New-Object Windows.Forms.Button;$script:Check.Text='重新检查';$script:Check.SetBounds(24,407,125,42)
    $script:Install=New-Object Windows.Forms.Button;$script:Install.Text='下载并安装缺少组件';$script:Install.SetBounds(162,407,245,42);$script:Install.Enabled=$false
    $script:Launch=New-Object Windows.Forms.Button;$script:Launch.Text='打开工坊';$script:Launch.SetBounds(420,407,145,42);$script:Launch.Enabled=$false
    $cancel=New-Object Windows.Forms.Button;$cancel.Text='取消 / 退出';$cancel.SetBounds(578,407,156,42)
    $note=New-Object Windows.Forms.Label;$note.Text='Windows 10／11 64 位 · Python 官方下载 · 浏览器请自行安装 · 下载失败可重新检查后重试';$note.SetBounds(24,466,715,45)
    $script:Form.Controls.AddRange(@($title,$script:Status,$script:Progress,$script:Details,$script:Check,$script:Install,$script:Launch,$cancel,$note))
    $script:Check.Add_Click({Run-Action {Check-Environment}})
    $script:Install.Add_Click({Run-Action {Install-Components}})
    $script:Launch.Add_Click({Run-Action {Start-Workshop}})
    $cancel.Add_Click({if($script:Busy){$script:StopRequested=$true;$script:Form.Tag='close'}else{$script:Form.Close()}})
    $script:Form.Add_FormClosing({param($sender,$e) if($script:Busy -and $script:Form.Tag -ne 'success'){$script:StopRequested=$true;$script:Form.Tag='close';$e.Cancel=$true}})
    $timer=New-Object Windows.Forms.Timer;$timer.Interval=200
    $timer.Add_Tick({$timer.Stop();Run-Action {Check-Environment}})
    $script:Form.Add_Shown({$timer.Start()})
    [Windows.Forms.Application]::Run($script:Form)
    $mutex.ReleaseMutex();$mutex.Dispose()
} catch { [Windows.Forms.MessageBox]::Show($_.Exception.Message,'帕琪工坊启动失败')|Out-Null }
