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
  NormalizedDataDir: String;
  DefaultDataDir: String;
  TempRoot: String;
  VerificationRoot: String;
  VerificationRootName: String;
  SafeToDelete: Boolean;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{param:HUBDATADIR|}');
    if DataDir = '' then
      DataDir := ExpandConstant('{localappdata}\PersonalHub');
    if DeleteUserDataRequested() then
    begin
      NormalizedDataDir := ExpandFileName(DataDir);
      while (Length(NormalizedDataDir) > 3) and
        (Copy(NormalizedDataDir, Length(NormalizedDataDir), 1) = '\') do
        Delete(NormalizedDataDir, Length(NormalizedDataDir), 1);

      DefaultDataDir := ExpandFileName(ExpandConstant('{localappdata}\PersonalHub'));
      while (Length(DefaultDataDir) > 3) and
        (Copy(DefaultDataDir, Length(DefaultDataDir), 1) = '\') do
        Delete(DefaultDataDir, Length(DefaultDataDir), 1);

      SafeToDelete := CompareText(NormalizedDataDir, DefaultDataDir) = 0;
      if not SafeToDelete then
      begin
        { The release lifecycle harness uses this narrowly scoped temporary layout.
          Arbitrary /HUBDATADIR paths are never recursively deleted. }
        TempRoot := ExpandFileName(GetTempDir());
        while (Length(TempRoot) > 3) and (Copy(TempRoot, Length(TempRoot), 1) = '\') do
          Delete(TempRoot, Length(TempRoot), 1);

        VerificationRoot := ExtractFileDir(NormalizedDataDir);
        VerificationRootName := ExtractFileName(VerificationRoot);
        SafeToDelete :=
          (CompareText(ExtractFileName(NormalizedDataDir), 'PersonalHub') = 0) and
          (CompareText(ExpandFileName(ExtractFileDir(VerificationRoot)), TempRoot) = 0) and
          (Length(VerificationRootName) > Length('PersonalHubInstallerVerification-')) and
          (CompareText(
            Copy(VerificationRootName, 1, Length('PersonalHubInstallerVerification-')),
            'PersonalHubInstallerVerification-') = 0);
      end;

      if SafeToDelete then
        DelTree(NormalizedDataDir, True, True, True)
      else
        MsgBox(
          'Personal Hub data was preserved because the requested cleanup path is not a recognized Personal Hub data directory.',
          mbInformation,
          MB_OK
        );
    end;
  end;
end;
