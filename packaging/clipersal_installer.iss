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
;
; The "installffmpeg" task (on by default) offers to install FFmpeg at setup time via
; winget. This is deliberately a DOWNLOAD at install time, not bundling: the verified
; full Windows ffmpeg build is GPL/nonfree, and shipping that binary inside this
; installer would pull the project under GPL redistribution obligations -- see
; ARCHITECTURE.md's "Why ffmpeg is not bundled". A winget-installed ffmpeg carries
; none of that.

#define MyAppName "Clipersal"
#define MyAppVersion "0.1.1-beta"
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
VersionInfoVersion=0.1.1.0
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
Name: "installffmpeg"; Description: "Install FFmpeg automatically (recommended -- required for capture)"; GroupDescription: "Additional setup:"

[Files]
Source: "..\dist\Clipersal\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\Clipersal-Trigger.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Clipersal"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall Clipersal"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Clipersal"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,Clipersal}"; Flags: nowait postinstall skipifsilent

[Code]
// FFmpeg handling for the installffmpeg task -- see the header comment at the
// top of this script for why this downloads at install time instead of bundling.

function FFmpegOnPath(): Boolean;
var
  ResultCode: Integer;
begin
  // Exec itself returns False when the process can't be launched at all (the
  // "guard"): Result then stays False. A nonzero exit code (9009) means cmd
  // ran fine but ffmpeg wasn't found on PATH.
  Result := Exec(ExpandConstant('{cmd}'), '/c ffmpeg -version', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep <> ssPostInstall then
    exit;
  if not WizardIsTaskSelected('installffmpeg') then
  begin
    Log('installffmpeg task not selected; skipping the FFmpeg install offer');
    exit;
  end;
  if FFmpegOnPath() then
  begin
    Log('FFmpeg is already on PATH; nothing to install');
    exit;
  end;
  Log('FFmpeg not found on PATH; attempting winget install of Gyan.FFmpeg');
  if Exec(ExpandConstant('{cmd}'),
      '/c winget install --id Gyan.FFmpeg -e --silent --accept-package-agreements --accept-source-agreements',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0) then
  begin
    Log('winget installed FFmpeg successfully');
    exit;
  end;
  Log(Format('FFmpeg install via winget failed or winget unavailable (exit code %d)', [ResultCode]));
  if MsgBox('Clipersal needs FFmpeg to capture, but it could not be installed automatically.' #13#10#13#10
      'Open the FFmpeg download page in your browser so you can install it yourself?',
      mbConfirmation, MB_YESNO) = IDYES then
    ShellExec('open', 'https://ffmpeg.org/download.html', '', '', SW_SHOWNORMAL, ewNoWait, ResultCode);
end;
