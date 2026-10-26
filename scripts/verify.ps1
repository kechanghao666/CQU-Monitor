$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw '请先运行scripts/bootstrap.ps1。' }
$env:PYTHONUTF8 = '1'
$env:PYQTGRAPH_QT_LIB = 'PySide6'
$audit = Join-Path $projectDir ('artifacts\verify_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
New-Item -ItemType Directory -Path $audit | Out-Null
Push-Location -LiteralPath $projectDir
try {
    & $pythonExe -m pip check
    if ($LASTEXITCODE -ne 0) { throw '依赖一致性检查失败' }
    & $pythonExe -m ruff check cqu_monitor tests scripts packaging/hooks run_app.py
    if ($LASTEXITCODE -ne 0) { throw '静态检查失败' }
    & $pythonExe -m ruff format --check cqu_monitor tests scripts packaging/hooks run_app.py
    if ($LASTEXITCODE -ne 0) { throw '格式检查失败' }
    & $pythonExe -m pytest tests -q --basetemp (Join-Path $audit 'pytest') --junitxml (Join-Path $audit 'tests.xml')
    if ($LASTEXITCODE -ne 0) { throw '自动测试失败' }
    & $pythonExe -m cqu_monitor --check-startup --output (Join-Path $audit 'startup')
    if ($LASTEXITCODE -ne 0) { throw '无设备启动检查失败' }
    Write-Host "验证完成，证据保存在：$audit"
} finally { Pop-Location }
