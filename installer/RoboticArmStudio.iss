; Inno Setup script for Robotic Arm Studio.
;   "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" installer\RoboticArmStudio.iss
; build.bat runs this automatically when Inno Setup is installed.
;
; Input is the PyInstaller --onedir build in build\app\RoboticArmStudio\ - the exe
; plus its _internal folder. Packaging the folder rather than a --onefile exe is
; what keeps Defender quiet (see the comment at the top of build.bat); the user
; never sees the folder, they see one Setup.exe and a Start menu entry.

#define AppName        "Robotic Arm Studio"
#define AppVersion     "1.0.0"
#define AppExe         "RoboticArmStudio.exe"

[Setup]
AppId={{FBA3250E-C2DC-44E2-9C71-DB79E7B5D13C}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Robotic Arm Studio
DefaultDirName={autopf}\Robotic Arm Studio
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExe}
SetupIconFile=..\app\assets\icon.ico
OutputDir=..\dist
OutputBaseFilename=RoboticArmStudio-{#AppVersion}-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; Default to a per-user install so no admin password is needed, but offer the
; all-users choice on the first page for anyone who has one.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Files]
Source: "..\build\app\RoboticArmStudio\{#AppExe}";   DestDir: "{app}";                       Flags: ignoreversion
Source: "..\build\app\RoboticArmStudio\_internal\*"; DestDir: "{app}\_internal";            Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md";                             DestDir: "{app}";                       Flags: ignoreversion
Source: "..\docs\WIRING.md";                        DestDir: "{app}\docs";                  Flags: ignoreversion
Source: "..\firmware\robotic_arm\robotic_arm.ino";  DestDir: "{app}\firmware\robotic_arm";  Flags: ignoreversion
Source: "..\sequences\*.json";                      DestDir: "{app}\sequences";             Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}";                   Filename: "{app}\{#AppExe}"
Name: "{autoprograms}\{#AppName} - Arduino sketch";  Filename: "{app}\firmware\robotic_arm"
Name: "{autoprograms}\{#AppName} - Wiring notes";    Filename: "{app}\docs\WIRING.md"
Name: "{autodesktop}\{#AppName}";                    Filename: "{app}\{#AppExe}";  Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

; Uninstall leaves %LOCALAPPDATA%\RoboticArmStudio alone - the user's recorded
; sequences and joint calibration outlive the program.
