@echo off
setlocal
set "APPDIR=%~dp0"
set "DATADIR=%ProgramData%\IOT\EdgeDevUpdater"
set "VENV=%DATADIR%\venv"
if not exist "%DATADIR%" mkdir "%DATADIR%"
if not exist "%DATADIR%\logs" mkdir "%DATADIR%\logs"
if not exist "%VENV%\Scripts\python.exe" (
  py -3 -m venv "%VENV%" || exit /b 1
  "%VENV%\Scripts\python.exe" -m pip install --quiet -r "%APPDIR%requirements.txt" || exit /b 1
)
cd /d "%APPDIR%"
"%VENV%\Scripts\python.exe" -m tools.dev_updater %*
endlocal
