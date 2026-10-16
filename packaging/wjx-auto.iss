; wjx-auto 安装包脚本（Inno Setup 6）
;
; 编译：
;   "C:\Users\<你>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" packaging\wjx-auto.iss
; 或直接用项目脚本（推荐）：
;   python tools\build_exe.py --setup
;
; 设计要点
;   * 按用户安装到 %LOCALAPPDATA%\Programs\wjx-auto，无需管理员权限
;   * 解包安装（installer 内部是 LZMA2 压缩的 onedir 目录），不是单文件运行时解包
;   * 卸载/覆盖安装时**保留 data\**（任务配置与答案库不会丢）
;   * 可选：桌面图标、开机自动后台运行

#define MyAppName "问卷星自动填写助手"
#define MyAppNameEn "wjx-auto"
#define MyAppPublisher "wjx-auto"
#define MyAppExeName "wjx-auto-gui.exe"
#define MyAppCliName "wjx-auto.exe"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
; 目录名用英文，避免中文路径带来的各种坑
#define MyAppDirName "wjx-auto"

[Setup]
AppId={{7E3B1C5A-9D42-4F18-9B77-2A6E5C8D1F30}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}
; 默认按用户安装到用户自己的目录 —— 这样不需要管理员权限也能装成功
DefaultDirName={localappdata}\Programs\{#MyAppDirName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 按用户安装（不请求 UAC）。如需装到 Program Files，可用 /ALLUSERS 启动安装包，会正常请求提权。
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
; 解包安装（压缩安装包内容）
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
OutputDir=..\dist\setup
OutputBaseFilename={#MyAppNameEn}-{#MyAppVersion}-setup
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupLogging=yes
; 允许安装到已存在的目录（覆盖升级）
AllowNoIcons=yes
DisableDirPage=no
; 若程序正在运行，安装前自动关掉它（覆盖安装时避免文件占用报错）
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "cn"; MessagesFile: "languages\ChineseSimplified.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce
Name: "autostart"; Description: "开机自动在后台运行（最小化到托盘，到点自动填写）"; GroupDescription: "附加任务："
Name: "quicklaunch"; Description: "创建「预演本卷」快捷方式（只读题不提交，方便核对答案库）"; GroupDescription: "附加任务："

[Files]
; 整个 onedir 目录：exe + _internal（运行时与依赖）+ drivers（浏览器驱动）
Source: "..\dist\wjx-auto\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 用户数据目录：只创建空目录，升级时不覆盖已有内容
Source: "..\packaging\空数据目录说明.txt"; DestDir: "{app}\data"; DestName: "说明.txt"; \
    Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
; WIDGET_NO_SHORTCUTS=1 时不创建任何快捷方式（受限环境/无人值守测试用）
#ifndef WIDGET_NO_SHORTCUTS
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\{#MyAppName}（命令行）"; Filename: "{app}\{#MyAppCliName}"; WorkingDir: "{app}"
Name: "{group}\环境自检（check-env）"; Filename: "{app}\{#MyAppCliName}"; Parameters: "check-env"; WorkingDir: "{app}"
Name: "{group}\使用说明"; Filename: "{app}\使用说明.txt"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{autoprograms}\预演本卷（只读题不提交）"; Filename: "{app}\{#MyAppCliName}"; \
    Parameters: "resolve --url ""https://v.wjx.cn/vm/mYv3i3D.aspx"""; WorkingDir: "{app}"; Tasks: quicklaunch
#endif

[Run]
; 装机后可选：立刻启动
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载时只删日志与运行期临时文件；data\ 留给用户（避免误删答案库）
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\run"
Type: filesandordirs; Name: "{app}\.selenium-cache"

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';

// ---------------------------------------------------------------------------
// 目录可写性检查：非管理员把程序装到 Program Files 之类的位置时，
// Inno 原来的报错是英文的「Cannot create directory / Error 5」，用户看不懂。
// 这里在向导阶段就检查，并给出"改成哪个目录"的中文提示。
// ---------------------------------------------------------------------------
// 通过"实际创建并删除一个临时目录"来判断目标位置是否可写。
// 不用 TFileStream.SaveToFile（Pascal Script 里没有这个方法），
// 也不依赖任何外部命令，CreateDir/RemoveDir 在 Inno 里是原生支持的。
function DirLooksWritable(const Dir: String): Boolean;
var
  Probe, Sub: String;
  Guard: Integer;
begin
  Result := False;
  Probe := Dir;
  Guard := 0;
  // 目录还不存在时，向上找到最近的已存在祖先再试
  while (Probe <> '') and (not DirExists(Probe)) and (Guard < 32) do
  begin
    Probe := ExtractFileDir(Probe);
    Guard := Guard + 1;
  end;
  if (Probe = '') or (not DirExists(Probe)) then
    Exit;

  Sub := AddBackslash(Probe) + '.wjx_write_test';
  if DirExists(Sub) then
    RemoveDir(Sub);
  if CreateDir(Sub) then
  begin
    RemoveDir(Sub);
    Result := True;
  end;
end;

function InstallDirHint(): String;
begin
  Result := ExpandConstant('{localappdata}\Programs\{#MyAppDirName}');
end;

// 进入"准备安装"前拦一下：目标目录不可写就明确告诉用户怎么改
function NextButtonClick(CurPageID: Integer): Boolean;
var
  Dir: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    Dir := WizardDirValue;
    if not DirLooksWritable(Dir) then
    begin
      if MsgBox('这个目录当前用户没有写入权限：' + #13#10 + Dir + #13#10 + #13#10 +
                '常见原因是选择了 C:\Program Files 这类需要管理员权限的位置。' + #13#10 + #13#10 +
                '建议改用当前用户的目录（确定后自动填入）：' + #13#10 +
                '    ' + InstallDirHint() + #13#10 + #13#10 +
                '若确实要装到系统目录，请关闭安装程序，' + #13#10 +
                '改用「以管理员身份运行」或命令行加 /ALLUSERS 重新安装。',
                mbConfirmation, MB_OKCANCEL) = IDOK then
      begin
        WizardForm.DirEdit.Text := InstallDirHint();
      end;
      Result := False;   // 回到本页，让用户确认新目录
    end;
  end;
end;

// 安装前最后一道保险：再检查一次，避免向导之后权限又变化
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not DirLooksWritable(WizardDirValue) then
    Result := '无法写入安装目录：' + WizardDirValue + #13#10 +
              '请改用当前用户可写的目录（例如 ' + InstallDirHint() + '），' +
              '或以管理员身份重新运行安装包。';
end;

// 开机自启：写注册表 Run 项（比启动文件夹更规范，卸载时也容易清理）
// 注意启动参数用 --minimized：程序会识别并直接收进托盘；写成别的位置参数会被当成未知子命令。
// WIDGET_NO_REGISTRY=1 时跳过（受限环境/无人值守测试用）
procedure SetAutoStart(AutoStart: Boolean);
var
  AppPath: String;
begin
#ifdef WIDGET_NO_REGISTRY
  Exit;
#else
  AppPath := ExpandConstant('{app}\{#MyAppExeName}');
  if AutoStart then
    RegWriteStringValue(HKCU, RunKey, '{#MyAppDirName}', '"' + AppPath + '" --minimized')
  else
    RegDeleteValue(HKCU, RunKey, '{#MyAppDirName}');
#endif
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if WizardIsTaskSelected('autostart') then
      SetAutoStart(True)
    else
      SetAutoStart(False);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    SetAutoStart(False);
end;
