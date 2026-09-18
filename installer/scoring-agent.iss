; Inno Setup script for Scoring Agent
; Build:  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\scoring-agent.iss
; Produces installer\Output\ScoringAgent-Setup-<ver>.exe with a licence (EULA) page,
; developer info, Start-Menu + Desktop shortcuts, and an uninstaller.

#define AppName        "Scoring Agent"
#define AppVersion     "1.3.0"
#define AppPublisher   "Hafiz Muhammad Ahsan"
#define AppExe         "ScoringAgent.exe"

[Setup]
AppId={{A9C3E7F1-2C4B-4E8A-9F1D-5C0A7B2E9D10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription=Scoring Agent - automated scoring-script publisher
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\Scoring Agent
DefaultGroupName=Scoring Agent
DisableProgramGroupPage=yes
LicenseFile=EULA.txt
InfoBeforeFile=
OutputDir=Output
OutputBaseFilename=ScoringAgent-Setup-{#AppVersion}
SetupIconFile=..\assets\scoring-agent.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; An update while the app is open would leave half-replaced files behind.
CloseApplications=yes
RestartApplications=no
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[InstallDelete]
; A onedir bundle must not blend with a previous version's libraries (or with the old
; single-file build's leftovers), so clear the payload before laying down the new one.
Type: filesandordirs; Name: "{app}\_internal"
Type: files;          Name: "{app}\ScoringAgent.exe"

[Files]
; onedir build: ship the whole dist\ScoringAgent folder (run build_app.ps1 first).
; onedir is deliberate - the old onefile exe unpacked ~48 MB on every launch and the window
; sat "Not Responding" for ~25 s. This starts in about 2 s.
Source: "..\dist\ScoringAgent\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "EULA.txt";          DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md";      DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\Scoring Agent";        Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall Scoring Agent"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Scoring Agent";  Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[UninstallDelete]
; SECURITY: the saved sign-in (Chrome profile + session marker) must not survive an uninstall.
Type: filesandordirs; Name: "{localappdata}\Scoring Agent"
Type: filesandordirs; Name: "{app}\_internal"

[Run]
; Windows Defender scans every newly written file the FIRST time it is loaded - ~55 MB
; across 580 files, which stalls the app window on its first launch and paints it
; "(Not Responding)". Reading them here pays that cost during setup, where a progress
; bar is expected, so the first launch is clean. Harmless if it fails.
; NOTE: no curly braces in the command - Inno reads "{" as the start of a constant.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command ""Get-ChildItem -LiteralPath '{app}' -Recurse -File -ErrorAction SilentlyContinue | Get-FileHash -ErrorAction SilentlyContinue | Out-Null"""; StatusMsg: "Preparing Scoring Agent for its first launch..."; Flags: runhidden waituntilterminated skipifdoesntexist

Filename: "{app}\{#AppExe}"; Description: "Launch Scoring Agent"; Flags: nowait postinstall skipifsilent
