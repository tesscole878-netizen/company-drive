@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0storage.local.json" set "DRIVE_DATA_DIR=%~dp0work\dev-drive-data"
set "DRIVE_HOST=127.0.0.1"
set "DRIVE_PORT=8765"
echo Starting Company Drive with the configured storage.
call "%~dp0start.cmd"
