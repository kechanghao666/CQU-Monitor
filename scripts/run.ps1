$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectDir '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw '独立环境缺失，请先运行bootstrap.ps1。' }
Start-Process -FilePath $pythonExe -ArgumentList @('-m', 'cqu_monitor') -WorkingDirectory $projectDir -WindowStyle Hidden
