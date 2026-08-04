@echo off
rem IOT Edge Development Updater launcher.
rem
rem Provisions a private virtual environment under %ProgramData%\IOT\EdgeDevUpdater
rem and starts the updater on port 8791. This venv is this application's own; the
rem Legacy Edge Upgrade Webapp keeps its .gateway-update-venv inside its Git
rem checkout and is not touched by anything here.
setlocal
set "APPDIR=%~dp0"
set "DATADIR=%ProgramData%\IOT\EdgeDevUpdater"
set "VENV=%DATADIR%\venv"

if not exist "%DATADIR%" mkdir "%DATADIR%"
if not exist "%DATADIR%\logs" mkdir "%DATADIR%\logs"

if not exist "%VENV%\Scripts\python.exe" (
  echo Preparing the Development Updater environment ^(first run only^)...
  py -3 -m venv "%VENV%" || (
    echo.
    echo Could not create the Python environment. Install Python 3.10 or newer
    echo from https://www.python.org/downloads/windows/ and run this again.
    pause
    exit /b 1
  )
  "%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip
  "%VENV%\Scripts\python.exe" -m pip install --quiet -r "%APPDIR%requirements.txt" || (
    echo.
    echo Could not install dependencies. Check this machine's internet access
    echo or proxy settings, then run this again.
    pause
    exit /b 1
  )
)

cd /d "%APPDIR%"
"%VENV%\Scripts\python.exe" -m tools.dev_updater %*
if errorlevel 2 (
  echo.
  echo The Development Updater did not start. See the message above.
  pause
)
endlocal
