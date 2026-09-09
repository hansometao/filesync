; Inno Setup 脚本：生成 filesync 安装包
; 用法：iscc /DMyAppVersion=1.1.0 installer.iss
;       或 python build.py --installer（自动传版本号）
; 需 Inno Setup 6（https://jrsoftware.org/isinfo.php）
; 产物：dist\installer\filesync-<版本>-setup.exe

#ifndef MyAppVersion
  #define MyAppVersion "1.1.0"
#endif

#define MyAppName "filesync"
#define MyAppExeName "folder_sync.exe"
#define MyAppPublisher "开发团队"

[Setup]
; 固定 AppId：升级安装时据此识别旧版本
AppId={{7C2F5E8A-3B6D-4C9E-9F1A-5D8E2B7A4C3F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; config/ 与 logs/ 生成在 exe 同目录，必须装到用户可写位置（无需管理员权限），
; 升级时默认沿用旧安装目录，用户配置与日志保留
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\filesync
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=dist\installer
OutputBaseFilename=filesync-{#MyAppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UsePreviousAppDir=yes

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："
Name: "autostart"; Description: "开机自动启动（后台运行）"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
; 单文件 exe 整体安装；用户数据（config/logs）在安装目录下，
; 升级安装用 ignoreversion 覆盖 exe，不动 config/ 与 logs/ 子目录
Source: "dist\folder_sync.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; 快捷方式引用 exe，显示 exe 自带图标（app.ico 已在 spec 中挂入）
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; 开机自启（可选任务）：以 --autostart 进入后台运行，不弹主窗口。
; 值名 FolderSync 与应用内 autostart.py 的 _REG_VALUE 一致（HKCU 同一键），
; 应用内「文件 → 开机自启」可正确识别/取消此注册；卸载时一并清除
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "FolderSync"; ValueData: """{app}\{#MyAppExeName}"" --autostart"; \
    Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent
