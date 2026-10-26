param([string]$InstallDir = (Join-Path $PSScriptRoot '外部贴标环境'))
$ErrorActionPreference='Stop'
try {
    $env:PIP_DISABLE_PIP_VERSION_CHECK='1'
    [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
    Write-Host 'PATCHI 外部贴标 CPU 安装（不修改工坊 Python 环境）'
    Write-Host '会下载 ComfyUI、CPU PyTorch、两个视觉模型。请保持网络连接，预留至少 8GB 磁盘。'
    $InstallDir=[IO.Path]::GetFullPath($InstallDir)
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    $py=Get-Command py -ErrorAction SilentlyContinue
    $python=Get-Command python -ErrorAction SilentlyContinue
    if (!(Test-Path (Join-Path $InstallDir 'venv\Scripts\python.exe'))) {
        if ($py) { & $py.Source -3 -m venv (Join-Path $InstallDir 'venv') }
        elseif ($python) { & $python.Source -m venv (Join-Path $InstallDir 'venv') }
        else { throw '未找到 Python。已有 Python 3.14 可以先直接使用；若 PyTorch 没有对应版本的 wheel，仅为此外部环境使用受支持的 Python。' }
        if ($LASTEXITCODE -ne 0) { throw '创建外部虚拟环境失败，查看上方 Python 错误。' }
    }
    $envPython=Join-Path $InstallDir 'venv\Scripts\python.exe'
    $comfy=Join-Path $InstallDir 'ComfyUI'
    if (!(Test-Path (Join-Path $comfy 'main.py'))) {
        $archive=Join-Path $InstallDir 'ComfyUI.zip'
        Write-Host '下载官方 ComfyUI 源码…'
        Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/Comfy-Org/ComfyUI/archive/refs/heads/master.zip' -OutFile $archive
        Expand-Archive -LiteralPath $archive -DestinationPath $InstallDir -Force
        Move-Item -LiteralPath (Join-Path $InstallDir 'ComfyUI-master') -Destination $comfy
        Remove-Item -LiteralPath $archive
    }
    & $envPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'pip 更新失败。' }
    & $envPython -m pip install 'torch>=2.6' torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
    if ($LASTEXITCODE -ne 0) { throw 'CPU PyTorch 安装失败：检查网络及当前 Python 的 wheel 支持。工坊本体无需更换 Python。' }
    & $envPython -m pip install -r (Join-Path $comfy 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'ComfyUI 依赖安装失败。' }
    $node=Join-Path $comfy 'custom_nodes\ComfyUI_PatchiSticker'
    New-Item -ItemType Directory -Path $node -Force | Out-Null
    Copy-Item -Path (Join-Path $PSScriptRoot 'ComfyUI_PatchiSticker\*') -Destination $node -Recurse -Force
    & $envPython -m pip install -r (Join-Path $node 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw '贴标节点依赖安装失败。' }
    & $envPython (Join-Path $node 'prepare_models.py')
    if ($LASTEXITCODE -ne 0) { throw '模型下载未完成，请按教程重试；无需重复安装所有环境。' }
    $startFile=Join-Path $InstallDir '启动外部贴标_CPU.bat'
    [IO.File]::WriteAllText($startFile,"@echo off`r`ncd /d `"%~dp0ComfyUI`"`r`n`"%~dp0venv\Scripts\python.exe`" main.py --cpu --listen 127.0.0.1 --port 8188`r`npause`r`n",[Text.Encoding]::ASCII)
    Write-Host '安装完成。请运行外部环境文件夹中的「启动外部贴标_CPU.bat」。' -ForegroundColor Green
} catch { Write-Host ('安装未完成：'+$_.Exception.Message) -ForegroundColor Red; exit 1 }
