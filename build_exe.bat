@echo off
rem Builds NBA19_Server.exe (single file, no Python needed to run it). Requires: pip install pyinstaller
cd /d "%~dp0"
python -m PyInstaller --noconfirm --clean --onefile --console --name NBA19_Server servidor.py
echo The exe is in the dist folder; put redirector.crt and redirector.key next to it.
pause
