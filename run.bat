@echo off
setlocal
cd /d "%~dp0"

set "PY=python"
where py >nul 2>nul
if %errorlevel%==0 set "PY=py"

%PY% -c "import serial" >nul 2>nul
if errorlevel 1 (
    echo Installing pyserial...
    %PY% -m pip install pyserial
)

%PY% "app\main.py" %*
if errorlevel 1 pause
