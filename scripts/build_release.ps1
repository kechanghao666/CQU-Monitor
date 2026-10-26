$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectDir '.venv\Scripts\python.exe'
$compiler = Join-Path $projectDir 'tools\InnoSetup6\app\ISCC.exe'
$extractor = Join-Path $projectDir 'tools\innoextract-1.9\innoextract.exe'
$releaseDir = Join-Path $projectDir 'release'
$appVersion = & $pythonExe -c "import runpy,sys; print(runpy.run_path(sys.argv[1])['__version__'])" (Join-Path $projectDir 'cqu_monitor\__init__.py')
if ($LASTEXITCODE -ne 0 -or -not $appVersion) { throw '无法读取当前版本' }
$buildDir = Join-Path $projectDir 'build\application'
foreach ($file in @($pythonExe, $compiler, $extractor)) {
    if (-not (Test-Path -LiteralPath $file)) { throw "缺少构建工具：$file" }
}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYQTGRAPH_QT_LIB = 'PySide6'
Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
Remove-Item Env:QT_PLUGIN_PATH -ErrorAction SilentlyContinue
Remove-Item Env:QML2_IMPORT_PATH -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null
New-Item -ItemType Directory -Path $buildDir -Force | Out-Null
$bundle = Join-Path $releaseDir 'CQU-Monitor'
if (Test-Path -LiteralPath $bundle) {
    # 保留旧程序及它可能含有的数据，避免PyInstaller覆盖时删除用户记录。
    $runningBundle = Get-CimInstance Win32_Process -Filter "Name = 'CQU-Monitor.exe'" | Where-Object { $_.ExecutablePath -eq (Join-Path $bundle 'CQU-Monitor.exe') }
    if ($runningBundle) { throw '请先正常退出正在运行的软件，再重新构建。' }
    $archiveBundle = Join-Path $projectDir ('artifacts\revisions\bundle_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
    $resolvedBundle = [System.IO.Path]::GetFullPath($bundle)
    $resolvedArchive = [System.IO.Path]::GetFullPath($archiveBundle)
    $resolvedProject = [System.IO.Path]::GetFullPath($projectDir).TrimEnd('\') + '\'
    if (-not ($resolvedBundle.StartsWith($resolvedProject, [StringComparison]::OrdinalIgnoreCase) -and $resolvedArchive.StartsWith($resolvedProject, [StringComparison]::OrdinalIgnoreCase))) { throw '旧程序备份路径超出本工程，已停止。' }
    New-Item -ItemType Directory -Path (Split-Path -Parent $archiveBundle) -Force | Out-Null
    Move-Item -LiteralPath $resolvedBundle -Destination $resolvedArchive
}
Push-Location -LiteralPath $projectDir
try {
    & $pythonExe -m PyInstaller --noconfirm --onedir --windowed --noupx `
        --name CQU-Monitor --distpath $releaseDir --workpath $buildDir --specpath $buildDir `
        --additional-hooks-dir (Join-Path $projectDir 'packaging\hooks') `
        --add-data ((Join-Path $projectDir 'cqu_monitor\assets') + ';cqu_monitor\assets') `
        --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PySide2 `
        --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets `
        --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick `
        --exclude-module PySide6.QtNetwork `
        (Join-Path $projectDir 'run_app.py')
    if ($LASTEXITCODE -ne 0) { throw '独立程序构建失败' }
    $bundle = Join-Path $releaseDir 'CQU-Monitor'
    foreach ($unusedQt in @('Qt6VirtualKeyboard.dll','Qt6Pdf.dll','Qt6Qml.dll','Qt6Quick.dll','Qt6Network.dll','opengl32sw.dll')) {
        if (Test-Path -LiteralPath (Join-Path $bundle ('_internal\PySide6\' + $unusedQt))) { throw "发行包意外包含未使用的Qt模块：$unusedQt" }
    }
    if (Test-Path -LiteralPath (Join-Path $bundle '_internal\libssl-3-x64.dll')) { throw '发行包混入非项目Python来源的OpenSSL运行库' }
    & $pythonExe (Join-Path $PSScriptRoot 'collect_notices.py') (Join-Path $bundle 'licenses')
    if ($LASTEXITCODE -ne 0) { throw '依赖版本及版权文件收集失败' }
    Copy-Item -LiteralPath (Join-Path $projectDir 'docs\使用说明.md') -Destination $bundle -Force
    Copy-Item -LiteralPath (Join-Path $projectDir 'docs\使用说明.html') -Destination $bundle -Force
    $exe = Join-Path $bundle 'CQU-Monitor.exe'
    $frozenOutput = Join-Path $projectDir ('artifacts\acceptance\frozen_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
    $process = Start-Process -FilePath $exe -ArgumentList @('--check-startup', '--output', ('"' + $frozenOutput + '"')) -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "独立程序验收失败：$frozenOutput" }
    $frozenReport = Get-Content -LiteralPath (Join-Path $frozenOutput 'report.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not ($frozenReport.passed -and $frozenReport.frozen)) { throw '独立程序验收报告未通过' }
    $installerDir = Join-Path $releaseDir 'installer'
    New-Item -ItemType Directory -Path $installerDir -Force | Out-Null
    & $compiler "/DBundleDir=$bundle" "/DOutputDir=$installerDir" "/DAppVersion=$appVersion" (Join-Path $projectDir 'packaging\app.iss')
    if ($LASTEXITCODE -ne 0) { throw '安装包编译失败' }
    $setup = Join-Path $installerDir "CQU-Monitor-$appVersion-Windows-x64-Setup.exe"
    $extracted = Join-Path $projectDir ('artifacts\installer_payload_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
    & $extractor --extract --output-dir $extracted $setup
    if ($LASTEXITCODE -ne 0) { throw '安装包提取检查失败' }
    $extractedExe = Join-Path $extracted 'app\CQU-Monitor.exe'
    if ((Get-FileHash -LiteralPath $exe).Hash -ne (Get-FileHash -LiteralPath $extractedExe).Hash) { throw '包内程序与独立版不一致' }
    $payloadOutput = Join-Path $projectDir ('artifacts\acceptance\installer_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
    $payload = Start-Process -FilePath $extractedExe -ArgumentList @('--check-startup', '--output', ('"' + $payloadOutput + '"')) -WindowStyle Hidden -Wait -PassThru
    if ($payload.ExitCode -ne 0) { throw "安装包内程序验收失败：$payloadOutput" }
    $payloadReport = Get-Content -LiteralPath (Join-Path $payloadOutput 'report.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not ($payloadReport.passed -and $payloadReport.frozen)) { throw '安装包内程序验收报告未通过' }
    $report = [ordered]@{
        checkedAt = (Get-Date -Format o)
        passed = $true
        version = $appVersion
        executable = $exe
        installer = $setup
        installerSHA256 = (Get-FileHash -LiteralPath $setup).Hash
        frozenAcceptance = $frozenOutput
        extractedPayloadAcceptance = $payloadOutput
        systemInstallationPerformed = $false
        realHardwareTested = $false
        continuous24hTested = $false
    }
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $releaseDir 'build-report.json') -Encoding UTF8
    $report | ConvertTo-Json -Depth 5
} finally {
    Pop-Location
}
