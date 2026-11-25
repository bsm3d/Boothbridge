; Boothbridge
; © Benoit Saint-Moulin - 2026
; Version du fichier : 1.0.0
; Fonction du fichier : Inno Setup script that installs the portable build in Program Files, with the publisher, version and icon details.

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
; so the program folder is emptied first. Settings, log and cache are in %APPDATA%\BSM\Boothbridge, never in runtime
[InstallDelete]
Type: filesandordirs; Name: "{app}\runtime"

[Files]
Source: "..\dist\Boothbridge\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autoprograms}\{#AppName} manual"; Filename: "{app}\Boothbridge-manual.pdf"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

; Nothing in [UninstallDelete]: the installed program writes nothing beside the exe (only the portable zip does). The user data
; in %APPDATA%\BSM\Boothbridge are handled by the Yes or No question at the end of this script.

; ---- user data question (added by the audit, group 2) ----
[CustomMessages]
english.DataAsk=Remove all the user data of %1?%n%nFolder(s):%n%2%nContent: %3 (%4 MB).%n%nYes: the folder(s) are deleted, for good.%nNo: they are kept, you get them back if you reinstall %1.%n%nYour data in D:\Datas is never touched: delete it yourself if you do not want it.
english.DataWhat=the settings and the log (the analyses are in D:\Datas\Boothbridge)

[Code]
// Size of a folder in bytes: the dialog tells the user what "Yes" removes.
function FolderBytes(const Dir: String): Int64;
var
  Rec: TFindRec;
begin
  Result := 0;
  if FindFirst(Dir + '\*', Rec) then
  try
    repeat
      if (Rec.Name <> '.') and (Rec.Name <> '..') then
      begin
        if (Rec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
          Result := Result + FolderBytes(Dir + '\' + Rec.Name)
        else
          Result := Result + (Int64(Rec.SizeHigh) * 4294967296) + Rec.SizeLow;
      end;
    until not FindNext(Rec);
  finally
    FindClose(Rec);
  end;
end;

procedure AddIfExists(var Dirs: TArrayOfString; const Dir: String);
var
  N: Integer;
begin
  if DirExists(Dir) then
  begin
    N := GetArrayLength(Dirs);
    SetArrayLength(Dirs, N + 1);
    Dirs[N] := Dir;
  end;
end;

// One question, Yes or No, No by default: remove all the user data? The program and its technical leftovers are always removed.
// A silent uninstall (/SILENT, /VERYSILENT) asks nothing and keeps the data: /SUPPRESSMSGBOXES does not answer a MsgBox of
// this script, so the uninstaller would wait for a click nobody can make.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Dirs: TArrayOfString;
  Where: String;
  Bytes: Int64;
  I: Integer;
begin
  if CurUninstallStep <> usPostUninstall then Exit;
  if UninstallSilent then Exit;
  SetArrayLength(Dirs, 0);
  AddIfExists(Dirs, ExpandConstant('{userappdata}\BSM\Boothbridge'));
  if GetArrayLength(Dirs) = 0 then Exit;
  Where := '';
  Bytes := 0;
  for I := 0 to GetArrayLength(Dirs) - 1 do
  begin
    if Where <> '' then Where := Where + #13#10;
    Where := Where + Dirs[I];
    Bytes := Bytes + FolderBytes(Dirs[I]);
  end;
  if MsgBox(FmtMessage(CustomMessage('DataAsk'), ['Boothbridge', Where, CustomMessage('DataWhat'), IntToStr(Integer(Bytes div 1048576))]),
            mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
  begin
    for I := 0 to GetArrayLength(Dirs) - 1 do
      DelTree(Dirs[I], True, True, True);
    RemoveDir(ExpandConstant('{userappdata}\BSM'));       // only when empty
  end;
end;
