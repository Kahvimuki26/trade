"""What start.bat / start.command run: update, install packages, open the dashboard.

When the dashboard's "Update" button installs a new version, the dashboard exits
with RESTART_CODE and this launcher starts it again with the new code.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

from . import updater

RESTART_CODE = 3
ROOT = updater.ROOT
RESUME_FILE = ROOT / ".tradebot-resume"


def deps_marker() -> Path:
    return Path(sys.prefix) / ".tradebot-deps"


def install_packages(force: bool = False) -> None:
    """pip install the project, but only when pyproject.toml changed (keeps startup fast)."""
    digest = hashlib.sha256((ROOT / "pyproject.toml").read_bytes()).hexdigest()
    marker = deps_marker()
    if not force and marker.exists() and marker.read_text() == digest:
        return
    print("  Installing / updating packages (one moment)...")
    code = subprocess.call([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                            "-e", ".[alpaca]"], cwd=ROOT)
    if code == 0:
        marker.write_text(digest)
    else:
        print("  Warning: package install failed (offline?). Trying to start anyway...")


def main() -> int:
    os.chdir(ROOT)
    print()
    updater.main(["--quiet"])
    install_packages()
    first = True
    while True:
        cmd = [sys.executable, "-m", "tradebot", "web"]
        if first:
            cmd.append("--open")
        if RESUME_FILE.exists():  # paper trader was running before an update-restart
            RESUME_FILE.unlink()
            cmd.append("--start")
        env = {**os.environ, "TRADEBOT_LAUNCHER": "1"}
        try:
            code = subprocess.call(cmd, env=env)
        except KeyboardInterrupt:
            return 0
        if code != RESTART_CODE:
            return code
        print("\n  Restarting with the new version...\n")
        install_packages()
        first = False


if __name__ == "__main__":
    sys.exit(main())
