; Coin11 助手轻量版 —— Inno Setup 安装器脚本（0.4.0）
;
; 编译：ISCC.exe Coin11Helper-lite.iss（参数 /DAppVersion、/DSourceDir）
; 或由 scripts\build-lite.ps1 -MakeInstaller 调用。
;
; 安装器只带：桌面壳、私有 ADB、出厂种子、Python 3.12 基座与下载组件；
; 用户可在软件内完成依赖下载/校验/安装/修复。绝不安装到硬编码开发机路径；
; 默认安装目录可由用户在向导中选择。

#ifndef AppVersion
  #define AppVersion "0.4.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\Coin11助手轻量版"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

[Setup]
AppId={{7B1E9A2C-4D6F-4E11-A9C0-2E5D8F1A3B6C}
AppName=Coin11助手（轻量版）
AppVersion={#AppVersion}
AppVerName=Coin11助手 轻量版 {#AppVersion}
AppPublisher=Coin11Helper (non-official wrapper)
AppPublisherURL=https://github.com/czl0325/coin11-tb
DefaultDirName={autopf}\Coin11助手轻量版
DefaultGroupName=Coin11助手
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\Coin11助手.exe
UninstallDisplayName=Coin11助手（轻量版）
OutputBaseFilename=Coin11助手安装版-{#AppVersion}-windows-x64
OutputDir={#OutputDir}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
; 不强制管理员（应用可写目录运行；卸载只需删除自身目录）
PrivilegesRequired=lowest
; 允许用户选择安装目录
DisableDirPage=no
AlwaysShowDirOnReadyPage=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked
Name: "launchafter"; Description: "安装完成后运行 Coin11 助手"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
; 源目录内容（来源由 /DSourceDir 指定；绝不包含完整 site-packages/OCR 模型）
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Coin11助手"; Filename: "{app}\Coin11助手.exe"
Name: "{autodesktop}\Coin11助手"; Filename: "{app}\Coin11助手.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Coin11助手.exe"; Description: "启动 Coin11 助手"; Flags: nowait postinstall skipifsilent; Tasks: launchafter

; ---------------------------------------------------------------------------
; 卸载：应用更新/卸载不删除 %LOCALAPPDATA% 下的脚本、日志与运行时 ——
; 只有用户明确勾选“删除我的数据”才删除（见 CurUninstallStep 下方代码）。
; 以下注册确保卸载器不触碰用户数据目录。
[Code]
const
  DataAppName = 'Coin11Helper';

function ShouldDeleteData(): Boolean;
var
  Res: Integer;
begin
  Result := False;
  // 卸载最后一步询问是否删除用户数据（%LOCALAPPDATA%\Coin11Helper）
  Res := MsgBox('是否同时删除用户数据（脚本、日志与已下载的运行组件）？' + #13#10 + #13#10 + '位置：%LOCALAPPDATA%\' + DataAppName + #13#10 + #13#10 + '选择“否”可保留数据，便于重新安装后继续使用。' + #13#10 + '选择“是”将删除该目录（设置、同步的脚本、日志与运行时）。',
    mbConfirmation, MB_YESNO or MB_DEFBUTTON2);
  Result := (Res = IDYES);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if ShouldDeleteData() then
    begin
      DataDir := ExpandConstant('{localappdata}\' + DataAppName);
      if DirExists(DataDir) then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
