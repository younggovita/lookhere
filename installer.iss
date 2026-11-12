; Lookhere installer (Inno Setup). Built by build.py, which passes AppVersion and AppSrc.
; Per-user install, no admin rights needed. Installing a newer version over an older one
; replaces the app and keeps recordings and settings (%LOCALAPPDATA%\Lookhere).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{8B3C6E52-5D7A-4F0B-9C1E-7A4D2B9E1F63}
AppName=Lookhere
AppVersion={#AppVersion}
AppPublisher=Morgan Johnstone
DefaultDirName={localappdata}\Programs\Lookhere
DefaultGroupName=Lookhere
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=Lookhere-Setup-{#AppVersion}
SetupIconFile={#SourcePath}lookhere.ico
UninstallDisplayIcon={app}\Lookhere.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "autostart"; Description: "Start Lookhere when Windows starts"; GroupDescription: "Options:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Options:"; Flags: unchecked

[Files]
Source: "{#AppSrc}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; third-party licences (ffmpeg is GPL v3): see licenses\THIRD-PARTY-NOTICES.txt
Source: "{#LicSrc}\*"; DestDir: "{app}\licenses"; Flags: ignoreversion

[InstallDelete]
; libraries from the previous version (their file names change between versions, so they'd pile up)
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\Lookhere"; Filename: "{app}\Lookhere.exe"
Name: "{group}\Lookhere licences"; Filename: "{app}\licenses\THIRD-PARTY-NOTICES.txt"
Name: "{userdesktop}\Lookhere"; Filename: "{app}\Lookhere.exe"; Tasks: desktopicon

[Registry]
; same value the app's "Start with Windows" tray toggle reads and writes
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Lookhere"; ValueData: """{app}\Lookhere.exe"""; Tasks: autostart; Flags: uninsdeletevalue; Check: not IsUpdate
; "Start with Windows" switched on later from the app's Settings is removed on uninstall as well
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "Lookhere"; Flags: uninsdeletevalue dontcreatekey

[Run]
Filename: "{app}\Lookhere.exe"; Description: "Start Lookhere now"; Flags: nowait postinstall skipifsilent
; an auto-update runs silently, so start the new version here
Filename: "{app}\Lookhere.exe"; Flags: nowait; Check: IsUpdate

[Code]
function IsUpdate(): Boolean;
begin
  { set by the app's updater: Lookhere-Setup.exe /SILENT /UPDATE=1 }
  Result := ExpandConstant('{param:UPDATE|0}') = '1';
end;

procedure StopRunningApp();
var
  Code: Integer;
begin
  { An update replaces Lookhere.exe, so a running copy has to close first. }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM Lookhere.exe /F', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningApp();
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    StopRunningApp();
    { "Start with Windows" may have been switched on from the app's Settings after installing }
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Lookhere');
  end;
end;
