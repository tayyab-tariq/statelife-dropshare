; Inno Setup script for the Windows client (FR-UPD-01, FR-SHARE-07, OQ-49). Built by
; scripts/build_client.ps1, which passes /DAppVersion, /DSourceDir, /DOutputDir and /DIconFile.
;
; Per user, no admin rights: %LOCALAPPDATA%\Programs\StateLife DropShare. Registers the
; statelife-dropshare:// link scheme. The start-at-sign-in entry belongs to the app (OQ-44), so
; Setup only removes it on uninstall. Uninstall keeps the sync folder and asks about local data.
; A silent update (/VERYSILENT, from the app's updater) waits for DropShare to quit, then starts
; it again when done. While Setup runs it holds a mutex the app waits on, so DropShare started
; meanwhile neither runs from half-replaced files nor starts a second installer.

#ifndef AppVersion
  #error Pass /DAppVersion=x.y.z (scripts/build_client.ps1 does)
#endif

#define AppName "StateLife DropShare"
#define AppExe "DropShare.exe"
#define Scheme "statelife-dropshare"
; Held by DropShare while it runs, and by Setup while it runs (platform/windows/running.py).
#define RunningMutex "StateLife DropShare running"
#define SetupMutexName "StateLife DropShare setup"
#define RunKey "Software\Microsoft\Windows\CurrentVersion\Run"

[Setup]
; Never change the AppId: Windows identifies the installed app (and its upgrades) by it.
AppId={{B4317528-C0DD-453E-ADD5-39F49AC37304}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=StateLife
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
UsedUserAreasWarning=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=DropShare-{#AppVersion}-setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
SetupMutex={#SetupMutexName}
Compression=lzma2
SolidCompression=yes
; DropShare is never closed by force: Setup waits for it to stop syncing safely (InitializeSetup).
CloseApplications=no
RestartApplications=no

[InstallDelete]
; Our own bundled files from the previous version, so nothing stale is left beside the new ones.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"

[Registry]
; statelife-dropshare:// links open DropShare at the item (FR-SHARE-07).
Root: HKCU; Subkey: "Software\Classes\{#Scheme}"; ValueType: string; ValueName: ""; ValueData: "URL:{#AppName} link"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\{#Scheme}"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\{#Scheme}\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"",0"
Root: HKCU; Subkey: "Software\Classes\{#Scheme}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent
; After an update from the app (silent), DropShare starts again by itself.
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: WizardSilent

[Code]
function DropShareRunning(): Boolean;
begin
  Result := CheckForMutexes('{#RunningMutex}');
end;

{ Wait for DropShare to quit. Silently (an update from the app, which is quitting) for up to
  10 minutes while it stops syncing; otherwise ask the user to close it. }
function WaitForDropShare(Silent: Boolean; Action: String): Boolean;
var
  Waited: Integer;
begin
  Waited := 0;
  while DropShareRunning() and Silent and (Waited < 1200) do
  begin
    Sleep(500);
    Waited := Waited + 1;
  end;
  while DropShareRunning() and not Silent do
  begin
    if MsgBox('StateLife DropShare is running. Quit it from its icon in the notification area ' +
              '(right-click, Quit), then choose OK to ' + Action + '.',
              mbInformation, MB_OKCANCEL) = IDCANCEL then
    begin
      Result := False;
      Exit;
    end;
  end;
  Result := not DropShareRunning();
end;

function InitializeSetup(): Boolean;
var
  ExitCode: Integer;
begin
  Result := WaitForDropShare(WizardSilent, 'continue');
  { A silent update that has to give up leaves DropShare as it was. Start it (or bring the one
    still running forward) so syncing doesn't silently stay stopped; it offers the update again. }
  if not Result and WizardSilent and FileExists(ExpandConstant('{localappdata}\Programs\{#AppName}\{#AppExe}')) then
    Exec(ExpandConstant('{localappdata}\Programs\{#AppName}\{#AppExe}'), '', '', SW_SHOWNORMAL,
         ewNoWait, ExitCode);
end;

function InitializeUninstall(): Boolean;
begin
  Result := WaitForDropShare(UninstallSilent, 'uninstall it');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ExitCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    { Started at sign-in by the app's own setting (OQ-44): nothing left to start. }
    RegDeleteValue(HKEY_CURRENT_USER, '{#RunKey}', '{#AppName}');
    { The sync folder always stays. Local data only if the user says so (OQ-49): the app removes
      its own files and the saved sign-in, while it is still installed. }
    if not UninstallSilent and
       (MsgBox('Also remove DropShare''s settings, logs and saved sign-in from this computer?' + #13#10#13#10 +
               'Your files stay in your DropShare folder either way.',
               mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
      Exec(ExpandConstant('{app}\{#AppExe}'), '--remove-app-data', '', SW_HIDE,
           ewWaitUntilTerminated, ExitCode);
  end;
end;
