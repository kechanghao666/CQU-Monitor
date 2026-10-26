#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef BundleDir
  #error BundleDir is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif

[Setup]
AppId={{D1A3975D-8BFA-431D-BE47-63EF8A900701}
AppName=重庆大学电解水制氢系统
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\CQU-Monitor
DefaultGroupName=电解水制氢监测
PrivilegesRequired=lowest
MinVersion=10.0.17763
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=CQU-Monitor-{#AppVersion}-Windows-x64-Setup
Compression=lzma2/fast
SolidCompression=yes
SetupLogging=yes
UninstallDisplayName=重庆大学电解水制氢系统
UninstallDisplayIcon={app}\CQU-Monitor.exe
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[InstallDelete]
; 先清理旧版应用私有运行库，再复制本版，防止已移除的Qt模块在覆盖安装后残留。
; 仅限固定的_internal目录；不使用{app}\*，不触及data或用户导出文件。
; InstallDelete删除不提供旧运行库回滚。升级前应备份旧安装包及整data；
; 安装失败时先保留当前data，再重装本版或使用备份安装包恢复旧程序。
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\重庆大学电解水制氢系统"; Filename: "{app}\CQU-Monitor.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\重庆大学电解水制氢系统"; Filename: "{app}\CQU-Monitor.exe"; WorkingDir: "{app}"; Tasks: desktopicon

Name: "{group}\使用说明"; Filename: "{app}\使用说明.html"

[Run]
Filename: "{app}\CQU-Monitor.exe"; Description: "启动重庆大学电解水制氢系统"; Flags: nowait postinstall skipifsilent

; 数据目录不加入UninstallDelete，卸载不会主动删除用户实验记录。
