@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.12 or later is required.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  pause
  exit /b 1
)
echo Company Drive: http://127.0.0.1:8765
if exist "storage.local.json" (
  echo Using this server's configured NAS and local database.
) else (
  echo First setup key: data\state\setup.key
)
echo Press Ctrl+C to stop.
".venv\Scripts\python.exe" run.py
pause
