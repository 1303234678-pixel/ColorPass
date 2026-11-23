; ColorPass 安装程序（Inno Setup 6）
;
; 分工：
;   · 这里只负责「向导 + 复制文件 + 注册卸载条目」；
;   · 装进达芬奇、写 config.json、记版本，交给 ColorPass.exe setup（见
;     colorpass/postinstall.py）—— 那套规则在源码模式也要用，不能在 Pascal
;     里再写一遍。
;
; 编译（一般由 tools/build_installer.py 调用）：
;   ISCC.exe /DMyAppVersion=0.6.0 installer\ColorPass.iss

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

; 运行时目录。开发时可以覆盖成别处，方便只重打安装包。
#ifndef Payload
  #define Payload "..\dist\ColorPass"
#endif

#define MyAppName "ColorPass"
#define MyAppPublisher "ColorPass"
#define MyAppURL "https://github.com/1303234678-pixel/ColorPass"

[Setup]
; AppId 是这个产品在 Windows 里的身份：升级/卸载靠它认出「同一个人」。
; 一旦发出去就不要再改，否则控制面板里会出现两条、旧版也卸不干净。
AppId={{8B4F2A17-6C3D-4E59-9A2B-7D5C1E8F3A64}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} 安装程序 {#MyAppVersion}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}

; 装到和旧安装器完全一样的位置：面板的 config.json 里存的是绝对路径，
; 换目录会让已装好的面板找不到入口。
; 目录算在 DefaultBinDir 里（脚本区），因为它要和 colorpass/paths.py 用同一个
; 根：那边取的是 %LOCALAPPDATA% 环境变量，而 {localappdata} 走的是 Shell
; 文件夹，被重定向过的 profile 下这两者会不一样 —— 装出「程序在 A、配置在 B」
; 的分裂状态，面板起来就报找不到入口。
DefaultDirName={code:DefaultBinDir}
DefaultGroupName={#MyAppName}
DisableDirPage=auto
DisableProgramGroupPage=yes
AllowNoIcons=no

; 全程不需要管理员权限：跑在达芬奇里的面板和这个 exe 都是当前用户。
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

OutputDir=..\dist
OutputBaseFilename={#MyAppName}-Setup-{#MyAppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\ColorPass.exe
UninstallDisplayName={#MyAppName} {#MyAppVersion}

Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
WizardSizePercent=110
UsePreviousAppDir=yes
DirExistsWarning=no

; 达芬奇开着时，助手进程会占着 bin 里的 dll。CloseApplications 让安装程序
; 通过重启管理器请它们让路；下面的 InitializeSetup 里还会先 taskkill 一遍兜底。
CloseApplications=yes
RestartApplications=no

InfoBeforeFile=before.txt

[Languages]
Name: "chs"; MessagesFile: "lang\ChineseSimplified.isl"

[Messages]
; 完成页固定这几句。注册失败的情况由脚本区弹窗单独说，不塞进这里 ——
; 这个标签字数一多就会把复选框挤出去。
FinishedHeadingLabel=ColorPass 安装完成
FinishedLabel=打开达芬奇后，在「工作区 → Workflow Integrations」里就能看到 ColorPass。%n%n第一次用之前请完全退出并重新打开达芬奇，让它重新加载插件目录。

[Files]
Source: "{#Payload}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; {app} 由本产品独占，升级前先把运行时整个清掉：0.5.x 以前的 PyInstaller
; 安装器不登记文件清单，覆盖安装会把上一版的文件留在原处（实测残留
; python312.dll、libcrypto-3.dll，而这版运行时是 3.13）—— 新旧混着跑最容易
; 出「莫名导入失败」。只清这两个位置，unins000.* 留着：这次升级万一中途失败，
; 至少卸载入口还在。
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\ColorPass.exe"

[Icons]
; 同样不用 {group}/{userprograms}：那是 Shell 文件夹，profile 被重定向时会和
; 实际登录用户的开始菜单错位（程序装了、菜单里却没有）。按 %APPDATA% 拼，
; 和安装目录用的是同一套口径。
Name: "{code:StartMenuDir}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{code:StartMenuDir}\{#MyAppName} 数据目录"; Filename: "{code:DataDir}"

[UninstallRun]
; 先摘掉面板和配置，再让卸载程序删文件 —— 顺序反了会留下「菜单里还有
; ColorPass、点了却报错」的半残状态。
Filename: "{app}\ColorPass.exe"; Parameters: "setup --remove"; Flags: runhidden waituntilterminated; RunOnceId: "RemovePanels"

[UninstallDelete]
; 卸载程序只删自己登记过的文件，老安装器留下的那些它不碰（实测会剩
; python312.dll 这种孤儿，还有一个空 bin 目录）。{app} 由本产品独占，
; 整个目录一起删，卸完不留空壳。
Type: filesandordirs; Name: "{app}"

[Code]
var
  RegisterCode: Integer;
  RegisterDone: Boolean;

{ 换行用函数而不是字面 #13#10：ISPP 预处理会把「行首的 #」当成它自己的指令
  （#13#10 单独起一行时会被读成指令 13），所以这里不能把转义拆行。 }
function NL(): String;
begin
  Result := #13#10;
end;

{ 装到哪 —— 必须和 colorpass/paths.py 的 BASE\bin 完全一致。 }
function DefaultBinDir(Param: String): String;
var
  Root: String;
begin
  Root := GetEnv('LOCALAPPDATA');
  if Root = '' then
    Root := ExpandConstant('{localappdata}');
  Result := Root + '\ColorPass\bin';
end;

{ 数据目录（草稿缓存 / 报告 / 配置）—— 开始菜单里那个快捷方式指向它。 }
function DataDir(Param: String): String;
var
  Root: String;
begin
  Root := GetEnv('LOCALAPPDATA');
  if Root = '' then
    Root := ExpandConstant('{localappdata}');
  Result := Root + '\ColorPass';
end;

{ 开始菜单文件夹。用 %APPDATA% 拼，理由同上：Shell 文件夹被重定向时，
  默认的 group 常量会指到别的 profile 去，用户在开始菜单里找不到卸载入口。 }
function StartMenuDir(Param: String): String;
var
  Root: String;
begin
  Root := GetEnv('APPDATA');
  if Root = '' then
    Root := ExpandConstant('{userappdata}');
  Result := Root + '\Microsoft\Windows\Start Menu\Programs\ColorPass';
end;

{ 面板每点一次按钮、每次状态轮询都会起一个 ColorPass.exe。它们占着 bin 里的
  dll，覆盖时会报「无法替换文件」。这些进程随时可以重建，先全收掉最省事；
  装完面板会自己再起。 }
procedure StopHelpers();
var
  ResultCode: Integer;
begin
  Exec('taskkill.exe', '/F /IM ColorPass.exe', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
end;

function InitializeSetup(): Boolean;
begin
  StopHelpers();
  Result := True;
end;

function InitializeUninstall(): Boolean;
begin
  StopHelpers();
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  AppDir: String;
  Msg: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  AppDir := ExpandConstant('{app}');
  { 文件已经铺好了，这里只做「注册」：面板放进达芬奇、写 config.json、记版本。 }
  RegisterDone := Exec(AppDir + '\ColorPass.exe',
                       'setup --bin "' + AppDir + '"', AppDir, SW_HIDE,
                       ewWaitUntilTerminated, ResultCode);
  if RegisterDone then
    RegisterCode := ResultCode
  else
    RegisterCode := -1;

  if RegisterCode <> 0 then
  begin
    { 静默安装（面板里的「立即更新」）不能弹框 —— 那会卡住等用户点确定。 }
    if not WizardSilent then
    begin
      if RegisterCode = 1 then
      begin
        Msg := '文件已经装好，但没找到达芬奇的插件目录。' + NL() + NL() +
               '请先启动一次达芬奇（让它建好 Hello World 工程），' +
               '然后重新运行本安装包；已装的部分不会重复占空间。';
      end
      else
      begin
        Msg := '面板注册这一步失败了（退出码 ' + IntToStr(RegisterCode) + '）。' +
               NL() + NL() + '运行时本身已经装好；可以手动执行：' + NL() +
               AppDir + '\ColorPass.exe setup --bin "' + AppDir + '"';
      end;
      MsgBox(Msg, mbInformation, MB_OK);
    end;
    Log('setup --bin 退出码 ' + IntToStr(RegisterCode));
  end;
end;
