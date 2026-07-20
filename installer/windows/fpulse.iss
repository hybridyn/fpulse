; F-Pulse OSS — Inno Setup script
; Build:
;   ISCC.exe installer\windows\fpulse.iss
; Produces: installer\windows\output\FPulse-Setup-<ver>.exe

#define MyAppName "F-Pulse"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Hybridyn Data Labs"
#define MyAppURL "https://hybridyn.com/f-pulse"
#define MyAppExeName "fpulse.exe"
; ProjectRoot is relative to this .iss file
#define ProjectRoot "..\.."

[Setup]
; A unique AppId — DO NOT regenerate this between minor versions or
; the installer treats every release as a fresh install.
AppId={{8A4B5A57-F8E2-4F37-9D2C-7E9C3F4A2B11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/support
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\FPulse
DefaultGroupName=F-Pulse
DisableProgramGroupPage=yes
LicenseFile={#ProjectRoot}\LICENSE
OutputBaseFilename=FPulse-Setup-{#MyAppVersion}
OutputDir=output
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64
; 2026-06-08: fpulse.ico is now generated at installer/windows/icons/ by
; make-icon.ps1 (re-run it if the file is ever missing). The icon
; references below + in [Files] / [Icons] are live.
UninstallDisplayIcon={app}\icons\fpulse.ico
SetupIconFile=icons\fpulse.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "runatlogon";        Description: "Auto-start F-Pulse at every Windows logon (recommended)"; GroupDescription: "Background service:"; Flags: checkedonce
Name: "desktopicon";       Description: "Create a desktop shortcut";                                GroupDescription: "Additional icons:"
Name: "openafter";         Description: "Open F-Pulse in browser when install finishes";          GroupDescription: "Post-install:";    Flags: checkedonce

[Files]
; The frozen Python runtime + bundled backend, produced by PyInstaller
; into ..\..\dist\fpulse\ ahead of running ISCC.
Source: "{#ProjectRoot}\dist\fpulse\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The compiled frontend from npm run build
Source: "{#ProjectRoot}\frontend\dist\*"; DestDir: "{app}\frontend\dist"; Flags: ignoreversion recursesubdirs createallsubdirs
; Branding + docs
Source: "icons\fpulse.ico"; DestDir: "{app}\icons"; Flags: ignoreversion
Source: "{#ProjectRoot}\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectRoot}\LICENSE";   DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectRoot}\NOTICE";    DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
; Icons carry the F-Pulse brand icon (installer/windows/icons/fpulse.ico).
; The main shortcut opens the browser DIRECTLY — no Python, no fpulse.exe.
; If Edge is present it opens a chromeless --app window (no address bar);
; otherwise it falls back to the default browser. The background service
; already serves the port, so this is purely a viewer.
Name: "{group}\F-Pulse";                Filename: "{code:AppLauncher}"; Parameters: "{code:AppLauncherArgs}"; IconFilename: "{app}\icons\fpulse.ico"; Comment: "Open F-Pulse"
Name: "{group}\F-Pulse — Stop service"; Filename: "{cmd}"; Parameters: "/C ""{app}\{#MyAppExeName}"" uninstall-service && pause"
Name: "{group}\Uninstall F-Pulse";       Filename: "{uninstallexe}"
Name: "{commondesktop}\F-Pulse";        Filename: "{code:AppLauncher}"; Parameters: "{code:AppLauncherArgs}"; Tasks: desktopicon; IconFilename: "{app}\icons\fpulse.ico"; Comment: "Open F-Pulse"

[Run]
; Register the OS-native service so it survives reboot + auto-restarts.
; Uses our own cross-platform CLI so the Scheduled Task points at the
; bundled python, not a system python the user may not have.
Filename: "{app}\{#MyAppExeName}"; Parameters: "install-service --port 8001"; Flags: runhidden waituntilterminated; Tasks: runatlogon
; First-run: open the UI in the user's default browser
Filename: "{app}\{#MyAppExeName}"; Parameters: "service-status"; Flags: runhidden waituntilterminated; Tasks: runatlogon
; First-run: open the UI in the browser (default browser tab — simple).
Filename: "http://localhost:8001"; Flags: shellexec nowait postinstall skipifsilent; Description: "Open F-Pulse now"; Tasks: openafter

[UninstallRun]
; Stop and remove the service BEFORE deleting binaries, so the running
; uvicorn process doesn't hold file locks on the python runtime.
Filename: "{app}\{#MyAppExeName}"; Parameters: "uninstall-service"; Flags: runhidden waituntilterminated

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"
; Note: we DO NOT delete %LOCALAPPDATA%\FPulse\data on uninstall.
; That holds the user's pipelines + run history. Re-installing or
; updating preserves it. Manual removal is documented in the support
; URL above.

[Code]
// Locate Microsoft Edge so the shortcut can open a chromeless --app window
// directly (no Python, no fpulse.exe). Returns '' if Edge isn't found.
function EdgeExe(): String;
begin
  Result := ExpandConstant('{commonpf32}\Microsoft\Edge\Application\msedge.exe');
  if not FileExists(Result) then
    Result := ExpandConstant('{commonpf}\Microsoft\Edge\Application\msedge.exe');
  if not FileExists(Result) then
    Result := '';
end;

// Shortcut target: Edge if present, else the URL (opens the default browser).
function AppLauncher(Param: String): String;
begin
  if EdgeExe() <> '' then
    Result := EdgeExe()
  else
    Result := 'http://localhost:8001';
end;

// Shortcut arguments: --app for Edge (chromeless); empty for the URL fallback.
function AppLauncherArgs(Param: String): String;
begin
  if EdgeExe() <> '' then
    Result := '--app=http://localhost:8001'
  else
    Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  // Friendly confirmation so the user knows their data dir survives.
  if MsgBox(
    'This will remove F-Pulse from this machine and stop the background service.'#13#10#13#10
    'Your data dir (%LOCALAPPDATA%\FPulse\data) — including pipelines, run history, ' +
    'and managed tables — will be PRESERVED. To remove it too, delete that folder manually.'#13#10#13#10
    'Continue?',
    mbConfirmation, MB_YESNO) = IDYES
  then
    Result := True
  else
    Result := False;
end;
