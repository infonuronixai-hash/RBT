<#
    Stage everything a recipient needs and zip it.

    Called by build.bat; run it directly to re-zip without rebuilding:
        powershell -ExecutionPolicy Bypass -File tools\make_package.ps1

    The zip normally carries both ways to run - the installer and the ready-to-run
    folder - because on a locked-down machine one may be blocked while the other is
    not. On a build machine whose security policy blocks reading the freshly built
    exe (see the antivirus notes in README.md) the folder cannot be archived at all;
    the installer still can, because the exe inside it is compressed rather than
    stored as an executable. That case is reported, not failed, since the installer
    on its own is a complete delivery.
#>
param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [string]$Version = "1.0.0"
)
$ErrorActionPreference = "Stop"

$dist  = Join-Path $Root "dist"
$stage = Join-Path $dist "package"
$app   = Join-Path $Root "build\app\RoboticArmStudio"
$setup = Join-Path $dist "RoboticArmStudio-$Version-Setup.exe"

function Test-Readable([string]$path) {
    if (-not (Test-Path $path)) { return $false }
    try { $s = [IO.File]::OpenRead($path); $s.Close(); return $true } catch { return $false }
}

if (-not (Test-Readable $setup)) { throw "No readable installer at $setup - run build.bat first." }

if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

Copy-Item $setup $stage

$portable = Test-Readable (Join-Path $app "RoboticArmStudio.exe")
if ($portable) {
    Copy-Item -Recurse $app (Join-Path $stage "RoboticArmStudio")
} else {
    Write-Warning "Security policy blocks reading build\app\RoboticArmStudio\RoboticArmStudio.exe"
    Write-Warning "Zipping the installer only. It contains the same application."
}

New-Item -ItemType Directory -Force -Path (Join-Path $stage "docs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $stage "firmware\robotic_arm") | Out-Null
Copy-Item (Join-Path $Root "docs\WIRING.md") (Join-Path $stage "docs")
Copy-Item (Join-Path $Root "firmware\robotic_arm\robotic_arm.ino") (Join-Path $stage "firmware\robotic_arm")
Copy-Item (Join-Path $Root "README.md") $stage

$runWays = @"
1. RoboticArmStudio-$Version-Setup.exe
   Installs for the current user only, so it asks for no administrator
   password. Adds a Start menu entry, an uninstaller, and puts the wiring
   notes and the Arduino sketch on disk. Uninstall from Settings > Apps.
"@
if ($portable) {
    $runWays += @"

2. RoboticArmStudio\RoboticArmStudio.exe
   Runs as-is, no installation. Keep the whole folder together - the exe
   needs the _internal folder beside it.
"@
}

$readme = @"
Robotic Arm Studio $Version - Windows
=====================================

HOW TO RUN IT
-------------
$runWays

Settings and recorded motions go to
    %LOCALAPPDATA%\RoboticArmStudio\
Uninstalling does not delete them.

WHAT WINDOWS MAY SAY THE FIRST TIME
-----------------------------------
"Windows protected your PC - unknown publisher"
    Expected. The build is not code-signed. Click More info -> Run anyway.

"Access is denied", with no prompt at all
    A company-managed PC blocking unrecognised executables by policy, not a
    fault in the program. Event Viewer shows Event ID 1121 under
    Microsoft-Windows-Windows Defender/Operational. It needs an exclusion
    from whoever manages that machine - try an unmanaged PC instead.

TRYING IT WITHOUT AN ARM
------------------------
Start the app, switch SIMULATOR on in the top bar, and press CONNECT. Every
control, recording and playback works against a simulated arm.

WITH REAL HARDWARE
------------------
1. Flash firmware\robotic_arm\robotic_arm.ino to an Arduino Uno.
   It reports RDY RoboArm 1.0 at 115200 baud.
2. Power the servos from a SEPARATE 5-6 V supply rated 2 A or more, and tie
   its ground to the Arduino's ground. Six servos run off the Uno's USB 5 V
   will brown out the board and reset it mid-move. See docs\WIRING.md.
3. Start the app, pick the COM port, press CONNECT.

Full documentation is in README.md.
"@
Set-Content -Path (Join-Path $stage "READ-ME-FIRST.txt") -Value $readme -Encoding utf8

$zip = Join-Path $dist "RoboticArmStudio-$Version-windows.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -CompressionLevel Optimal
Remove-Item -Recurse -Force $stage

$mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Output "packaged $zip ($mb MB)"
if (-not $portable) { Write-Output "contents: installer + docs + firmware (no portable folder - see warning above)" }
