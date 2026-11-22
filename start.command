#!/bin/bash
# Mac/Linux: double-click (or run  bash start.command) to open the tradebot dashboard.
# It updates itself to the latest version every time you start it.
# If macOS says it can't be opened: right-click it -> Open.
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
  echo "First run: setting things up. This takes a minute..."
  if ! python3 -m venv .venv; then
    echo
    echo "Python 3 was not found. Install it from https://www.python.org/downloads/ and run this again."
    read -r -p "Press Enter to close."
    exit 1
  fi
fi
.venv/bin/python -m tradebot.launch
