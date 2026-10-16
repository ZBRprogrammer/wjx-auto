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
; 默认按用户安装到用户自己的目录 —— 这样不需要管理员权限也能装成功。
; 实际默认值会在 InitializeWizard 里按"哪个目录真的能建出来"动态决定。
DefaultDirName={localappdata}\{#MyAppDirName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; ---------------------------------------------------------------------------
; 权限策略（按用户实测结论调整）
;
; 用户环境（个人电脑 + 安全软件/受控文件夹访问）下，非管理员运行时连
; %TEMP%\is-XXXX.tmp 都写不进去，安装程序在"解包"阶段就报
; "安装程序无法创建目录 … 错误 5: 拒绝访问"。而管理员权限下一切正常。
;
; 因此：默认就请求提权（安装包启动时弹 UAC），装完再让用户以普通权限使用程序
;       —— 程序本身运行时不需要管理员权限。
; 仍然保留 /CURRENTUSER 开关：高级用户可以以当前用户身份静默安装（按用户目录）。
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline

; ---------------------------------------------------------------------------
; 让安装包**外壳**也要求管理员权限（真正的"运行前就弹 UAC"）
;
; 背景：Inno 生成的 setup.exe = 自解压外壳(SetupLdr) + 负载。外壳的清单默认是
; asInvoker，而它必须先把自身解包到 %TEMP%\is-XXXX.tmp —— 若该目录被安全软件/
; 受控文件夹访问拦住，就会在"申请提权之前"报
; 「安装程序无法创建目录 …\Temp\is-XXXX.tmp / 错误 5: 拒绝访问」（连 UAC 都不弹）。
;
; 解决：用 tools/patch_inno_launcher.py 把 Inno 安装目录里的 SetupLdr.e32/e64
; 清单改成 requireAdministrator（逐字节等长替换，文件大小不变；原文件有备份，
; --restore 可还原）。之后 ISCC 编译出的 setup.exe 外壳就带 requireAdministrator，
; Windows 会在启动安装包的第一时间弹 UAC，确认后全程管理员权限，临时目录写入不再受限。
;
; 因为改过预编译文件（SetupLdr），签名校验必然不通过，按官方文档关闭该校验。
; 取值需写明是哪一类文件：Inno 6.7 起 setupldre32/setupldre64 合并为 setupldr。
DisablePrecompiledFileVerifications=setupldr
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

[Dirs]
; 管理员模式默认装到 Program Files，而程序运行时是**普通用户权限**，
; 所以必须把这些运行期要写的目录放开权限，否则运行时会报"拒绝访问"。
; （程序自身也有回落机制：真的写不了时会改用 %LOCALAPPDATA%\wjx-auto。）
Name: "{app}\data"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{app}\logs"; Permissions: users-modify
Name: "{app}\run"; Permissions: users-modify
Name: "{app}\.selenium-cache"; Permissions: users-modify

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
  CandidateCount = 7;

// ---------------------------------------------------------------------------
// 目录可写性检查
//
// 关键点：必须**真正把路径建出来**再验证，而不是只测最近已存在的祖先。
// 之前只测祖先，导致 %LOCALAPPDATA%\Programs 这种"中间层建不出来"的情况漏过检查，
// 安装时才由 Inno 抛出英文的「Cannot create directory / Error 5」。
//
// 做法：从已存在的祖先开始，一层层 CreateDir 下去（记录新建了哪些），
// 再写一个临时子目录确认可写，最后把新建的层删掉（只删自己建的）。
// ---------------------------------------------------------------------------
function DirLooksWritable(const Dir: String): Boolean;
var
  Missing: TArrayOfString;
  Probe, Sub: String;
  Guard, I: Integer;
  Ok: Boolean;
begin
  Result := False;
  if Dir = '' then
    Exit;

  // 1) 收集"还不存在"的路径层级
  SetArrayLength(Missing, 0);
  Probe := Dir;
  Guard := 0;
  while (Probe <> '') and (not DirExists(Probe)) and (Guard < 64) do
  begin
    SetArrayLength(Missing, GetArrayLength(Missing) + 1);
    Missing[GetArrayLength(Missing) - 1] := Probe;
    Probe := ExtractFileDir(Probe);
    Guard := Guard + 1;
  end;
  if (Probe = '') or (not DirExists(Probe)) then
    Exit;

  // 2) 真正把它们建出来（下标大的更靠上层，需从上层往下建）
  Ok := True;
  for I := GetArrayLength(Missing) - 1 downto 0 do
  begin
    if not CreateDir(Missing[I]) then
    begin
      Ok := False;
      Break;
    end;
  end;

  // 3) 建成了 → 再写一个临时目录确认真的可写
  if Ok then
  begin
    Sub := AddBackslash(Dir) + '.wjx_write_test';
    if DirExists(Sub) then
      RemoveDir(Sub);
    if CreateDir(Sub) then
    begin
      RemoveDir(Sub);
      Result := True;
    end;
  end;

  // 4) 清理：只删本次自己建出来的层（从下往上）
  for I := 0 to GetArrayLength(Missing) - 1 do
    RemoveDir(Missing[I]);
end;

// ---------------------------------------------------------------------------
// 自诊断模式：  setup.exe /TESTDIRS
// 在向导打开前就把"本机哪些目录能建、哪些被拒"写成一份文本报告，
// 方便定位"安装程序无法创建目录 / 拒绝访问"这类问题。不安装任何东西。
// ---------------------------------------------------------------------------
function TestOneDir(const Title, Dir: String): String;
begin
  Result := Title + ' = ' + Dir + #13#10;
  if DirExists(Dir) then
    Result := Result + '    已存在；'
  else
    Result := Result + '    不存在；';
  if DirLooksWritable(Dir) then
    Result := Result + '可写 ✔' + #13#10
  else
    Result := Result + '不可写 ✘' + #13#10;
end;

function BoolText(Value: Boolean): String;
begin
  if Value then
    Result := '是'
  else
    Result := '否';
end;

// 候选安装目录（按可写性依次尝试）。
// 管理员模式下优先系统默认位置；按用户（/CURRENTUSER）时才走用户目录。
function CandidateDir(Index: Integer): String;
var
  UserProfile: String;
begin
  if IsAdminInstallMode then
  begin
    case Index of
      0: Result := ExpandConstant('{autopf}\{#MyAppDirName}');
      1: Result := ExpandConstant('{sd}\{#MyAppDirName}');
      2: Result := ExpandConstant('{localappdata}\{#MyAppDirName}');
    else
      Result := '';
    end;
    Exit;
  end;

  case Index of
    0: Result := ExpandConstant('{localappdata}\{#MyAppDirName}');
    1: Result := ExpandConstant('{localappdata}\Programs\{#MyAppDirName}');
    2: Result := ExpandConstant('{userappdata}\{#MyAppDirName}');
    3: Result := ExpandConstant('{userdocs}\{#MyAppDirName}');
    4: Result := ExpandConstant('{userdesktop}\{#MyAppDirName}');
    5: Result := ExpandConstant('{tmp}\{#MyAppDirName}');
    6:
      begin
        // 直接拼 USERPROFILE（不依赖 Inno 常量，兼容被重定向的环境）
        UserProfile := GetEnv('USERPROFILE');
        if UserProfile <> '' then
          Result := AddBackslash(UserProfile) + '{#MyAppDirName}'
        else
          Result := '';
      end;
  else
    Result := '';
  end;
end;

// ---------------------------------------------------------------------------
// 自诊断模式：  setup.exe /TESTDIRS
// 在向导打开前就把"本机哪些目录能建、哪些被拒"写成一份文本报告，
// 方便定位"安装程序无法创建目录 / 拒绝访问"这类问题。不安装任何东西。
// ---------------------------------------------------------------------------
procedure RunDirDiagnostics();
var
  Report: String;
  I: Integer;
  OutFile: String;
begin
  Report := 'wjx-auto 安装目录自诊断' + #13#10 +
            '时间: ' + GetDateTimeString('yyyy-mm-dd hh:nn:ss', '-', ':') + #13#10 +
            '安装包版本: {#MyAppVersion}' + #13#10 +
            'Windows: ' + GetWindowsVersionString + #13#10 +
            '是否管理员: ' + BoolText(IsAdminLoggedOn) + #13#10 +
            'USERPROFILE    = ' + GetEnv('USERPROFILE') + #13#10 +
            'LOCALAPPDATA   = ' + ExpandConstant('{localappdata}') + #13#10 +
            'APPDATA        = ' + ExpandConstant('{userappdata}') + #13#10 +
            'TEMP           = ' + ExpandConstant('{tmp}') + #13#10 +
            '用户桌面       = ' + ExpandConstant('{userdesktop}') + #13#10 +
            '用户文档       = ' + ExpandConstant('{userdocs}') + #13#10 + #13#10 +
            '—— 候选安装目录 ——' + #13#10;
  for I := 0 to CandidateCount - 1 do
    Report := Report + TestOneDir('候选' + IntToStr(I), CandidateDir(I));

  Report := Report + #13#10 + '—— 其它关键位置 ——' + #13#10 +
            TestOneDir('桌面', ExpandConstant('{userdesktop}')) +
            TestOneDir('用户文档', ExpandConstant('{userdocs}')) +
            TestOneDir('临时目录', ExpandConstant('{tmp}'));

  // 报告按"最可能成功且用户能找到"的顺序落盘
  OutFile := ExpandConstant('{userdesktop}\wjx-auto-安装目录自诊断.txt');
  if not SaveStringToFile(OutFile, Report, False) then
  begin
    OutFile := ExpandConstant('{userdocs}\wjx-auto-安装目录自诊断.txt');
    if not SaveStringToFile(OutFile, Report, False) then
    begin
      OutFile := ExpandConstant('{tmp}\wjx-auto-安装目录自诊断.txt');
      SaveStringToFile(OutFile, Report, False);
    end;
  end;
  Log('--- /TESTDIRS 自诊断报告开始 ---');
  Log(Report);
  Log('--- 报告结束 ---');

  MsgBox('自诊断完成，报告已保存到：' + #13#10 + OutFile + #13#10 + #13#10 +
         '请把该文件内容发回，即可定位是哪个目录被拒绝访问。',
         mbInformation, MB_OK);
end;

// 关键：Inno 需要先把安装包解包到 %TEMP%\is-XXXX.tmp。
// 如果这个临时目录不可写（受控文件夹访问 / 杀软拦截会这样），
// 就会在"安装程序无法创建目录 ...\Temp\is-XXXX.tmp / 错误 5"处直接失败，
// 连日志文件都写不出来。
// 本安装包默认以管理员权限运行（启动时弹 UAC），因此正常情况下不会走到这里；
// 若用户显式用 /CURRENTUSER 以普通权限运行、且临时目录确实不可写，
// 就提前拦住并给出可执行的建议。
// 早期探针：把当前进程的权限状态写到固定位置。
// 用途：当安装连"解包临时目录"都建不出来时，日志文件本身也写不到临时目录，
// 这个探针会告诉排查者"提权到底有没有发生"。
//   写了 → 安装程序成功跑到代码阶段（提权生效或本就不需要提权）
//   没写 → 连这一步都没到（提权没发生 / 安全软件拦截了进程）
//
// 注意调用时机：InitializeSetup 里 {app} 与 WizardForm 都还不存在，
// 所以本函数只能在 InitializeWizard（或更晚）调用。
procedure WriteEarlyProbe();
var
  Probe: String;
  Text: String;
  Target: String;
begin
  Target := WizardDirValue;
  Text := 'wjx-auto 安装早期探针' + #13#10 +
          '时间: ' + GetDateTimeString('yyyy-mm-dd hh:nn:ss', '-', ':') + #13#10 +
          '安装包版本: {#MyAppVersion}' + #13#10 +
          '是否管理员: ' + BoolText(IsAdmin) + #13#10 +
          '管理安装模式: ' + BoolText(IsAdminInstallMode) + #13#10 +
          'TEMP: ' + ExpandConstant('{tmp}') + #13#10 +
          'TEMP 可写: ' + BoolText(DirLooksWritable(ExpandConstant('{tmp}'))) + #13#10 +
          '安装目录: ' + Target + #13#10 +
          '安装目录可写: ' + BoolText(DirLooksWritable(Target)) + #13#10;
  Probe := ExpandConstant('{userdesktop}') + '\wjx-auto-安装探针.txt';
  if not SaveStringToFile(Probe, Text, False) then
  begin
    Probe := ExpandConstant('{src}\wjx-auto-安装探针.txt');
    SaveStringToFile(Probe, Text, False);
  end;
  Log('--- 早期探针（InitializeWizard 之后）---');
  Log(Text);
end;

function InitializeSetup(): Boolean;
var
  TmpOk: Boolean;
  Answer: Integer;
begin
  Result := True;

  // 显式请求自诊断：只诊断，不安装
  if Pos('TESTDIRS', UpperCase(GetCmdTail)) > 0 then
  begin
    RunDirDiagnostics();
    Result := False;
    Exit;
  end;

  // 管理员模式（本包默认）下临时目录一定可写，不必打扰
  if IsAdminInstallMode then
    Exit;

  // 静默安装时不打扰，交给命令行使用者自己判断
  if Pos('SILENT', UpperCase(GetCmdTail)) > 0 then
    Exit;

  TmpOk := DirLooksWritable(ExpandConstant('{tmp}'));
  if not TmpOk then
  begin
    Answer := MsgBox(
      '检测到系统的临时目录不可写，安装程序无法解包，因此不能继续安装：' + #13#10 +
      '    ' + ExpandConstant('{tmp}') + #13#10 + #13#10 +
      '常见原因是「Windows 安全中心 → 病毒和威胁防护 → 勒索软件防护（受控文件夹访问）」' + #13#10 +
      '或第三方安全软件（360、火绒、腾讯电脑管家等）拦截了对用户临时目录的写入。' + #13#10 + #13#10 +
      '本安装包默认会请求管理员权限；若你刚才用了 /CURRENTUSER，' + #13#10 +
      '请去掉该参数重新运行（安装包会弹出 UAC 提权，提权后即可正常安装）。' + #13#10 + #13#10 +
      '  · 确定：现在导出"目录可写性"诊断报告，便于进一步定位；' + #13#10 +
      '  · 取消：使用免安装的绿色版（推荐，完全不依赖临时目录）。',
      mbConfirmation, MB_OKCANCEL);
    if Answer = IDOK then
    begin
      RunDirDiagnostics();
    end
    else
    begin
      MsgBox('请改用绿色版：把发行包里的 dist\wjx-auto 整个文件夹拷到任意可写位置' + #13#10 +
             '（例如 D:\wjx-auto），双击 wjx-auto-gui.exe 即可，无需安装。',
             mbInformation, MB_OK);
    end;
    Result := False;
  end;
end;

// 首选安装目录（提示文案用）
function InstallDirHint(): String;
begin
  Result := CandidateDir(0);
end;

// 返回第一个可写候选目录；都不可写则返回空串。
// 命令行显式给了 /DIR=... 时不要覆盖用户的选择（PrepareToInstall 会再校验并明确报错）。
function PickWritableDir(): String;
var
  I: Integer;
  Pref: String;
begin
  // 向导里用户改过目录、或命令行传了 /DIR，就尊重它
  // （InitializeWizard 之前 WizardForm 还不存在，所以只在它建好后读 WizardDirValue）
  if (not IsUninstaller) and (WizardForm <> nil) then
  begin
    Pref := WizardDirValue;
    if (Pref <> '') and DirExists(Pref) and DirLooksWritable(Pref) then
    begin
      Result := Pref;
      Exit;
    end;
  end;
  for I := 0 to CandidateCount - 1 do
  begin
    Result := CandidateDir(I);
    if (Result <> '') and DirLooksWritable(Result) then
      Exit;
  end;
  Result := '';
end;

procedure InitializeWizard();
var
  Dir: String;
begin
  Dir := PickWritableDir();
  if Dir <> '' then
    WizardForm.DirEdit.Text := Dir;
  // 此刻 WizardForm 已创建、{app} 可用，适合写"提权有没有生效"的探针
  WriteEarlyProbe();
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

// 安装前最后一道保险：再检查一次，避免向导之后权限又变化；
// 同时给出"到底哪个目录失败、还能改哪个"的明确中文说明（替代 Inno 的英文 Error 5）
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Dir, Suggest: String;
begin
  Result := '';
  Dir := WizardDirValue;
  if DirLooksWritable(Dir) then
    Exit;

  Suggest := PickWritableDir();
  Result := '无法写入安装目录：' + #13#10 + '    ' + Dir + #13#10 + #13#10 +
            '原因通常是该目录需要管理员权限，或当前用户对它没有写权限。' + #13#10 + #13#10;
  if Suggest <> '' then
    Result := Result + '可以改用这个目录（用户自己的目录，无需管理员权限）：' + #13#10 +
              '    ' + Suggest + #13#10 + #13#10;
  Result := Result + '其它办法：' + #13#10 +
            '  · 点「上一步」回到目录选择页，手动改成上面建议的目录' + #13#10 +
            '  · 或右键安装包 →「以管理员身份运行」，装到系统目录' + #13#10 +
            '  · 或直接用免安装的绿色版目录（dist\wjx-auto，拷到任意位置双击 exe 即可）';
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
