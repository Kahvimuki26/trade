@echo off
rem Double-click to open the tradebot dashboard.
rem It updates itself to the latest version every time you start it.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo First run: setting things up. This takes a minute...
  python -m venv .venv
  if errorlevel 1 (
    echo.
    echo Python was not found. Install it from https://www.python.org/downloads/
    echo and tick "Add Python to PATH" in the installer. Then run this again.
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" -m tradebot.launch
pause
