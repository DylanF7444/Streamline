#define MyAppName "Personal Hub"
#define MyAppVersion "0.1.0"
#define MyAppExeName "PersonalHub.exe"
#define MyAppId "{A42B9C7D-8D03-4F7E-AD72-30DBE72F62A1}"

[Setup]
AppId={{#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Personal Hub
DefaultDirName={localappdata}\Programs\PersonalHub
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
OutputBaseFilename=PersonalHub-{#MyAppVersion}-Setup
OutputDir=..\dist\personal-hub
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UsePreviousAppDir=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "startup"; Description: "Start Personal Hub when I sign in"; Flags: unchecked

[Files]
Source: "..\dist\PersonalHub.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; AppUserModelID: "PersonalHub.Desktop"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "PersonalHub"; ValueData: """{app}\{#MyAppExeName}"""; Tasks: startup; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Personal Hub"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
function DeleteUserDataRequested(): Boolean;
var
  Parameter: String;
begin
  Parameter := ExpandConstant('{param:DELETEUSERDATA|ask}');
  if CompareText(Parameter, 'yes') = 0 then
    Result := True
  else if CompareText(Parameter, 'no') = 0 then
    Result := False
  else if WizardSilent then
    Result := False
  else
    Result := MsgBox(
      'Delete Personal Hub settings, tasks, cached summaries, watch history, credentials, logs, and backups?' + #13#10 + #13#10 +
      'Choose No to preserve all user data for a later reinstall.',
      mbConfirmation,
      MB_YESNO
    ) = IDYES;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{param:HUBDATADIR|}');
    if DataDir = '' then
      DataDir := ExpandConstant('{localappdata}\PersonalHub');
    if DeleteUserDataRequested() then
      DelTree(DataDir, True, True, True);
  end;
end;
