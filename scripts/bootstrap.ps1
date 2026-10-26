[CmdletBinding()]
param([string]$BasePython)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    # 优先使用显式指定的解释器，否则查找本机Python 3.11；不依赖原作者用户名。
    $candidates = @()
    if ($BasePython) { $candidates += $BasePython }
    if (-not $BasePython) {
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        if ($launcher) {
            $discovered = & $launcher.Source -3.11 -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0) { $candidates += [string]$discovered }
        }
        $command = Get-Command python -ErrorAction SilentlyContinue
        if ($command) { $candidates += $command.Source }
    }
    $selected = $null
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            & $candidate -c 'import sys,struct; assert sys.version_info[:2] == (3,11) and struct.calcsize("P") == 8' 2>$null
            if ($LASTEXITCODE -eq 0) { $selected = $candidate; break }
        }
    }
    if (-not $selected) { throw '需要64位Python 3.11。安装后重试，或使用 -BasePython 指定python.exe完整路径。' }
    & $selected -m venv (Join-Path $projectDir '.venv')
    if ($LASTEXITCODE -ne 0) { throw '创建独立开发环境失败' }
}
$lock = Join-Path $projectDir 'requirements.lock.txt'
$wheelhouse = Join-Path $projectDir 'wheelhouse'
if (-not (Test-Path -LiteralPath $lock)) { throw '缺少版本锁定文件，不安装不确定版本。' }
if (Test-Path -LiteralPath $wheelhouse) {
    & $pythonExe -m pip install --disable-pip-version-check --only-binary=:all: --no-index --find-links $wheelhouse -r $lock
} else {
    & $pythonExe -m pip install --disable-pip-version-check --only-binary=:all: --index-url https://pypi.org/simple -r $lock
}
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }
& $pythonExe -m pip check
if ($LASTEXITCODE -ne 0) { throw '依赖一致性检查失败' }
Write-Host '维护环境就绪。请先运行scripts/verify.ps1，再修改或构建软件。'
