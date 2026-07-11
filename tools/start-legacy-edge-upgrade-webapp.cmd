@echo off
setlocal
cd /d "%~dp0\.."

if not exist ".gateway-update-venv\Scripts\python.exe" (
  py -3 -m venv .gateway-update-venv
)

".gateway-update-venv\Scripts\python.exe" -m pip install -r tools\gateway-update-requirements.txt
".gateway-update-venv\Scripts\python.exe" tools\legacy_edge_upgrade_webapp.py
