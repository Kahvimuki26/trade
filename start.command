#!/bin/bash
# Mac: double-click to open the tradebot dashboard. First run sets everything up.
# If macOS says it can't be opened: right-click it -> Open, or run  bash start.command
cd "$(dirname "$0")" || exit 1

if [ ! -f .venv/bin/activate ]; then
  echo "First run: setting things up. This takes a minute..."
  if ! python3 -m venv .venv; then
    echo
    echo "Python 3 was not found. Install it from https://www.python.org/downloads/ and run this again."
    read -r -p "Press Enter to close."
    exit 1
  fi
fi
source .venv/bin/activate
python -m pip install -q --disable-pip-version-check -e ".[alpaca]" \
  || echo "Warning: could not install/update packages (offline?). Trying to start anyway..."

echo
echo "Opening the dashboard. Keep this window open while you use it. Close it to stop."
tradebot web --open
