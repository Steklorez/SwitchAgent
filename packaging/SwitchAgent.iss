; Inno Setup script -- builds the single SwitchAgent-Setup-x64.exe installer
; around the PyInstaller onedir bundle (see SwitchAgent.spec).
;
; Build locally with (from the project root, after `pyinstaller --noconfirm
; packaging/SwitchAgent.spec` has produced dist\SwitchAgent\):
;   python packaging\build_installer.py
;
; That wrapper computes /DMyAppVersion automatically from the installed
; package's own version -- do NOT invoke ISCC.exe directly with a
; hand-typed /DMyAppVersion=X.Y.Z; a stale copy-pasted version string
; there would silently desync the installer's AppVersion from the EXE's
; own embedded version resource (a real issue found in a release-
; readiness audit and fixed by adding that wrapper). MyAppVersion is
; still passed in via /D rather than hardcoded here, so the single source
; of truth stays pyproject.toml's [project] version (read at build time
; via importlib.metadata -- see switchagent/__init__.py); see
; docs/PACKAGING.md and .github/workflows/release-windows.yml, which
; computes it the same way. Falls back to 0.0.0 only if ISCC.exe is
; somehow invoked directly without /D, so that doesn't hard-fail either.
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "SwitchAgent"
#define MyAppPublisher "SwitchAgent project"
#define MyAppExeName "SwitchAgent.exe"
#define MyAppURL "https://github.com/"

[Setup]
; Fixed for the life of this project -- NEVER change this GUID across
; releases. Inno Setup uses it (not the version) to recognize "this is an
; upgrade of the same app" and correctly replace the previous Start
; Menu/Add-Remove-Programs entry rather than creating a second one.
AppId={{ED0A5E9F-ECE4-417B-9F65-5D8D8A468337}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
VersionInfoVersion={#MyAppVersion}

; Per-user install, no admin/UAC prompt ever -- no system-level component
; needs elevation (see docs/PACKAGING.md). {localappdata} is always
; writable by the current user without elevation.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes

OutputDir=..\dist\installer
OutputBaseFilename=SwitchAgent-Setup-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=..\switchagent\web\static\app.ico
LicenseFile=..\LICENSE

; LAN binding is enabled by default. Windows manages its own network
; permission prompt; this per-user installer does not modify firewall rules.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[InstallDelete]
; Inno Setup only ever adds/overwrites whatever [Files] lists -- it never
; diffs against what a PREVIOUS version put there, so a file dropped from
; a later release (a retired template, an old static asset, ...) would
; otherwise sit on disk forever as orphaned garbage after an in-place
; upgrade. Wiping the whole {app} tree first, on every install (fresh or
; upgrade), guarantees the result always matches exactly what this
; version's [Files] section lays down -- safe because {app} is ONLY
; program files (see the block at the end of this script); user data
; lives entirely under a separate %LOCALAPPDATA%\SwitchAgent and is never
; touched here.
Type: filesandordirs; Name: "{app}"

[Files]
; The full PyInstaller onedir bundle -- SwitchAgent.exe plus _internal\.
; recursesubdirs/createallsubdirs preserves the bundle's own internal
; layout exactly (switchagent/web/app.py's template/static lookup depends
; on that relative structure -- see switchagent/paths.py).
Source: "..\dist\SwitchAgent\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
; An update started from SwitchAgent's own banner (switchagent/app_update.py)
; runs this installer with /SILENT /RELAUNCH=1: start the new version again.
Filename: "{app}\{#MyAppExeName}"; Flags: nowait; Check: IsRelaunch

[Code]
function IsRelaunch: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

; ---------------------------------------------------------------------------
; IMPORTANT -- persistent user data survives install/upgrade/uninstall.
;
; Persistent application data (SQLite DB, config.yaml, work/staging,
; logs/) lives under %LOCALAPPDATA%\SwitchAgent (see switchagent/paths.py's
; app_data_root(), "installed" mode) -- a COMPLETELY SEPARATE directory
; from {app} (%LOCALAPPDATA%\Programs\SwitchAgent, the program files
; this installer manages). This script intentionally has NO
; [UninstallDelete] section and never references that data directory
; anywhere -- do not add one. An upgrade (re-running this installer with
; a newer version, same AppId) only overwrites {app}'s program files;
; uninstalling only removes {app}. The user's DB/config/job history are
; never touched by either operation.
; ---------------------------------------------------------------------------
