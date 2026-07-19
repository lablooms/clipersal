; Inno Setup script for Clipersal. Build the PyInstaller output first, then compile
; this with Inno Setup 6 (https://jrsoftware.org/isinfo.php):
;
;   pyinstaller packaging/clipersal.spec --clean
;   iscc packaging/clipersal_installer.iss
;
; Produces dist_installer/ClipersalSetup-<version>.exe. Packages the existing onedir
; Clipersal.exe build plus the standalone Clipersal-Trigger.exe -- no changes needed
; to clipersal.spec itself.
;
; MyAppVersion must be kept in sync with pyproject.toml's [project] version by hand.
; AppId is a fixed GUID -- never change it in a future version, since Windows uses it
; to recognize "this is an upgrade of the same app" rather than a separate install.

#define MyAppName "Clipersal"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "Lablooms"
#define MyAppURL "https://github.com/lablooms/clipersal"
#define MyAppExeName "Clipersal.exe"

[Setup]
AppId={{44B1DD17-BD77-42BF-8EF1-529C7E7C721C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
VersionInfoVersion=0.1.0.0
DefaultDirName={autopf}\Lablooms\Clipersal
DefaultGroupName=Clipersal
UninstallDisplayIcon={app}\{#MyAppExeName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENSE
SetupIconFile=..\assets\icon.ico
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
OutputDir=..\dist_installer
OutputBaseFilename=ClipersalSetup-{#MyAppVersion}
; Uses Windows Restart Manager to detect and offer to close a running Clipersal
; before overwriting its files -- covers both a fresh install over a running instance
; and an in-place upgrade.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\Clipersal\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\Clipersal-Trigger.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Clipersal"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall Clipersal"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Clipersal"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,Clipersal}"; Flags: nowait postinstall skipifsilent
