; SimPy Lab Studio installer. Compile with tools/build_installer.ps1.
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef AppExe
  #define AppExe "..\dist\SimPy Lab Studio.exe"
#endif
#ifndef WebView2Bootstrapper
  #define WebView2Bootstrapper "..\build\installer-deps\MicrosoftEdgeWebview2Setup.exe"
#endif
#ifndef SetupOutput
  #define SetupOutput "..\dist"
#endif

[Setup]
AppId={{69F4E6BE-D651-4A60-97F4-8D34D96A7D10}
AppName=SimPy Lab Studio
AppVersion={#AppVersion}
AppPublisher=SimPy Lab Studio Contributors
AppPublisherURL=https://github.com/Sylphiette666/SimPy-Lab-Studio
AppSupportURL=https://github.com/Sylphiette666/SimPy-Lab-Studio/issues
AppUpdatesURL=https://github.com/Sylphiette666/SimPy-Lab-Studio/releases
DefaultDirName={localappdata}\Programs\SimPy Lab Studio
DefaultGroupName=SimPy Lab Studio
DisableProgramGroupPage=yes
DisableDirPage=no
DisableWelcomePage=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0
AppMutex=Local\SimPyLabStudio.Desktop.1
SetupMutex=Local\SimPyLabStudio.Setup.1
CloseApplications=no
RestartApplications=no
RestartIfNeededByRun=no
UninstallDisplayIcon={app}\SimPy Lab Studio.exe
UninstallDisplayName=SimPy Lab Studio
OutputDir={#SetupOutput}
OutputBaseFilename=SimPy-Lab-Studio-Setup-{#AppVersion}
SetupIconFile=..\src\simlab\static\studio\app.ico
WizardStyle=modern
WizardImageFile=..\build\installer-deps\wizard-large.bmp
WizardSmallImageFile=..\build\installer-deps\wizard-small.bmp
WizardImageStretch=yes
LicenseFile=..\LICENSE
InfoBeforeFile=install-notes.txt
Compression=lzma2
SolidCompression=yes
LZMAUseSeparateProcess=yes
VersionInfoVersion={#AppVersion}.0
VersionInfoDescription=SimPy Lab Studio Windows Installer
VersionInfoProductName=SimPy Lab Studio

[Languages]
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopShortcut}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#WebView2Bootstrapper}"; Flags: dontcopy
Source: "install-notes.txt"; DestDir: "{app}"; DestName: "使用说明.txt"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\SimPy Lab Studio"; Filename: "{app}\SimPy Lab Studio.exe"; WorkingDir: "{app}"; AppUserModelID: "SimPy.Lab.Studio.Desktop"
Name: "{userdesktop}\SimPy Lab Studio"; Filename: "{app}\SimPy Lab Studio.exe"; WorkingDir: "{app}"; Tasks: desktopicon; AppUserModelID: "SimPy.Lab.Studio.Desktop"
Name: "{group}\{cm:UserGuide}"; Filename: "{app}\使用说明.txt"

[Run]
Filename: "{app}\SimPy Lab Studio.exe"; Description: "{cm:LaunchStudio}"; Flags: nowait postinstall skipifsilent

[CustomMessages]
chinesesimp.DesktopShortcut=创建桌面快捷方式
chinesesimp.AdditionalIcons=快捷方式：
chinesesimp.UserGuide=使用说明
chinesesimp.LaunchStudio=启动 SimPy Lab Studio
chinesesimp.InstallRuntime=正在安装 Microsoft WebView2，请保持网络连接并稍候…
chinesesimp.RuntimeFailed=未能安装 Microsoft WebView2。请检查网络连接或组织策略，然后重试。也可以先从微软官网下载 WebView2 Runtime 再运行本安装程序。
chinesesimp.RuntimeMissing=安装后仍未检测到 WebView2 Runtime。请重启 Windows 后重试，或先从微软官网安装 Runtime。
english.DesktopShortcut=Create a desktop shortcut
english.AdditionalIcons=Shortcuts:
english.UserGuide=User guide
english.LaunchStudio=Launch SimPy Lab Studio
english.InstallRuntime=Installing Microsoft WebView2. Keep the internet connection active and please wait...
english.RuntimeFailed=Microsoft WebView2 could not be installed. Check the internet connection or organization policies and retry. You may also install WebView2 Runtime from Microsoft before running this installer again.
english.RuntimeMissing=WebView2 Runtime was not detected after installation. Restart Windows and retry, or install the Runtime from Microsoft first.

[Code]
const
  RuntimeKey = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function ValidRuntimeAt(RootKey: Integer): Boolean;
var
  VersionString: String;
  Version: Int64;
begin
  Result := False;
  if RegQueryStringValue(RootKey, RuntimeKey, 'pv', VersionString) then
    if StrToVersion(VersionString, Version) then
      Result := ComparePackedVersion(Version, 0) > 0;
end;

function WebView2Installed: Boolean;
begin
  Result := ValidRuntimeAt(HKLM32) or ValidRuntimeAt(HKCU32) or ValidRuntimeAt(HKCU64);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  Launched: Boolean;
begin
  Result := '';
  if WebView2Installed then begin
    Log('WebView2 Runtime is already installed; bootstrapper skipped.');
    Exit;
  end;
  WizardForm.PreparingLabel.Caption := CustomMessage('InstallRuntime');
  WizardForm.Update;
  ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
  Launched := Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'),
    '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Log(Format('WebView2 bootstrapper returned %d.', [ResultCode]));
  if not Launched then begin
    Result := CustomMessage('RuntimeFailed');
    Exit;
  end;
  if WebView2Installed then begin
    NeedsRestart := ResultCode = 3010;
    Exit;
  end;
  if ResultCode = 3010 then begin
    NeedsRestart := True;
    Result := CustomMessage('RuntimeMissing');
  end else
    Result := CustomMessage('RuntimeFailed') + ' (' + IntToStr(ResultCode) + ')';
end;

// No [UninstallDelete] rule touches user data or the shared WebView2 Runtime.
