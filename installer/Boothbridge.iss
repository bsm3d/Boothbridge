; Benoit Saint-Moulin
; Boothbridge : Inno Setup script, install of the portable build in Program Files
; Build: ISCC.exe installer\Boothbridge.iss   (after python build.py)

#define AppName "Boothbridge"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppExe "Boothbridge.exe"
; the edition is added to the name of the setup, build.py gives it
#ifndef Edition
  #define Edition ""
#endif

[Setup]
AppId={{6ACF9846-E0D6-415D-9B8B-C845B4BBEDAE}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Benoit Saint-Moulin
AppPublisherURL=https://www.benoitsaintmoulin.com
VersionInfoVersion={#AppVersion}.0
; Program Files needs administrator rights. The settings, the log and the cache are in %APPDATA%\BSM\Boothbridge,
; nothing is written beside the exe, so a folder the user cannot write to is fine
PrivilegesRequired=admin
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=notice.txt
SetupIconFile=..\djapp\ui\res\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
OutputDir=..\dist
OutputBaseFilename=Boothbridge-{#AppVersion}-setup{#Edition}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

; a file left by an older version would be reported as unexpected by the startup check,
; so the program folder is emptied first. Settings and log sit next to the exe, not in runtime
[InstallDelete]
Type: filesandordirs; Name: "{app}\runtime"

[Files]
Source: "..\dist\Boothbridge\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{app}\boothbridge.log"
Type: files; Name: "{app}\boothbridge.json"
