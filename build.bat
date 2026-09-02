@echo off
rem Build the distributable.
rem
rem   build.bat              -> dist\RoboticArmStudio-1.0.0-Setup.exe  (what you hand out)
rem   build.bat portable     -> also dist\portable\RoboticArmStudio.exe (single file, no installer)
rem
rem The shipped build is --onedir, not --onefile. A onefile exe unpacks itself into
rem %TEMP% and runs from there, which is precisely the behaviour Defender's heuristics
rem flag - an unsigned one is routinely blocked with "Access is denied" on the machine
rem that just built it. The folder build does not, and the installer hides the folder
rem from the user anyway. The portable single file stays available for a USB stick,
rem with that caveat attached.
rem
rem Every path handed to PyInstaller is absolute (%~dp0). --specpath re-bases
rem relative ones against build\, where none of these files live.
setlocal
cd /d "%~dp0"

set "PY=python"
where py >nul 2>nul
if %errorlevel%==0 set "PY=py"

set "COMMON=--noconfirm --clean --windowed --name RoboticArmStudio"
set "COMMON=%COMMON% --icon "%~dp0app\assets\icon.ico""
set "COMMON=%COMMON% --version-file "%~dp0tools\version_info.txt""
set "COMMON=%COMMON% --paths "%~dp0app""
set "COMMON=%COMMON% --add-data "%~dp0sequences;sequences""
set "COMMON=%COMMON% --hidden-import ui --hidden-import paths --hidden-import serial.tools.list_ports"

echo === 1/5  build dependencies ===
%PY% -m pip install --disable-pip-version-check -q pyserial pillow pyinstaller
if errorlevel 1 goto fail

echo === 2/5  icon ===
%PY% "%~dp0tools\make_icon.py"
if errorlevel 1 goto fail

echo === 3/5  application (folder build) ===
%PY% -m PyInstaller %COMMON% --onedir ^
  --distpath "%~dp0build\app" --workpath "%~dp0build\work" --specpath "%~dp0build" ^
  "%~dp0app\main.py"
if errorlevel 1 goto fail

if /i "%~1"=="portable" (
    echo === 3b/5  single-file portable exe ===
    %PY% -m PyInstaller %COMMON% --onefile ^
      --distpath "%~dp0dist\portable" --workpath "%~dp0build\work-onefile" --specpath "%~dp0build" ^
      "%~dp0app\main.py"
    if errorlevel 1 goto fail
)

echo === 4/5  installer ===
rem Inno Setup lands in Program Files for an all-users install and under
rem %LOCALAPPDATA%\Programs for a per-user one, which is what winget does.
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo Inno Setup 6 not found - skipping the installer.
    echo Install it with:  winget install -e --id JRSoftware.InnoSetup
    echo The folder build in build\app\RoboticArmStudio\ still runs as-is.
    goto done
)
"%ISCC%" /Q "%~dp0installer\RoboticArmStudio.iss"
if errorlevel 1 goto fail

echo === 5/5  distribution zip ===
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\make_package.ps1"
if errorlevel 1 goto fail

:done
echo.
echo Build finished. Files in dist\:
dir /b /s "%~dp0dist"
exit /b 0

:fail
echo.
echo BUILD FAILED - see the messages above.
pause
exit /b 1
