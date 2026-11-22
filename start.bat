@echo off
rem Double-click to open the tradebot dashboard. First run sets everything up.
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
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
call ".venv\Scripts\activate.bat"
python -m pip install -q --disable-pip-version-check -e ".[alpaca]"
if errorlevel 1 echo Warning: could not install/update packages (offline?). Trying to start anyway...

echo.
echo Opening the dashboard. Keep this window open while you use it. Close it to stop.
tradebot web --open
pause
