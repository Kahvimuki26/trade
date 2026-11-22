"""Self-updater: pulls the latest version of the project from GitHub.

Standard library only, so it also works before the packages are installed.

Never touched by an update (your stuff):
  .env, configs/instances.yaml, state/, data/, results/, .venv/
Files you added yourself (e.g. your own strategy files) are left alone too.
Files that came with the project are replaced by the new version.

    python -m tradebot.updater          # update if a new version exists
    python -m tradebot.updater --check  # only check
"""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.request
import zipfile
from pathlib import Path

OWNER, REPO = "Kahvimuki26", "trade"
DEFAULT_BRANCH = "claude/chat-session-crakqk"
ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ".tradebot-version"
MANIFEST_FILE = ".tradebot-files"  # files that came with the installed version
PROTECTED = (
    ".env", ".venv/", ".git/", "state/", "data/", "results/",
    "configs/instances.yaml", "configs/instances.yaml.bak", VERSION_FILE, MANIFEST_FILE, ".tradebot-resume",
)
UA = {"User-Agent": "tradebot-updater"}


def branch() -> str:
    return os.environ.get("TRADEBOT_UPDATE_BRANCH") or DEFAULT_BRANCH


def _get(url: str, accept: str | None = None, timeout: float = 20) -> bytes:
    headers = dict(UA)
    if accept:
        headers["Accept"] = accept
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return r.read()


def latest_sha() -> str:
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/commits/{branch()}"
    return _get(url, accept="application/vnd.github.sha", timeout=10).decode().strip()


def download_zip(sha: str) -> bytes:
    return _get(f"https://codeload.github.com/{OWNER}/{REPO}/zip/{sha}", timeout=120)


def current_sha(root: Path = ROOT) -> str | None:
    f = root / VERSION_FILE
    return f.read_text().strip() or None if f.exists() else None


def is_git_checkout(root: Path = ROOT) -> bool:
    return (root / ".git").exists()


def check(root: Path = ROOT) -> dict:
    cur = current_sha(root)
    info = {"current": cur, "latest": None, "available": False, "branch": branch(),
            "git": is_git_checkout(root), "error": None}
    try:
        info["latest"] = latest_sha()
        info["available"] = info["latest"] != cur
    except Exception as e:  # offline, rate-limited, ...
        info["error"] = str(e)
    return info


def _protected(rel: str) -> bool:
    return any(rel == p or (p.endswith("/") and rel.startswith(p)) for p in PROTECTED)


def apply_zip(data: bytes, sha: str, root: Path = ROOT) -> dict:
    """Extract a GitHub source zip over ``root``. Returns counts of changed files."""
    changed, added, skipped, shipped = [], [], [], set()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            parts = info.filename.split("/", 1)
            if len(parts) < 2 or not parts[1] or info.is_dir():
                continue
            rel = parts[1]
            if rel.startswith("/") or ".." in Path(rel).parts:
                continue  # never write outside the project
            if _protected(rel):
                skipped.append(rel)
                continue
            shipped.add(rel)
            target = root / rel
            new = z.read(info)
            if target.exists() and target.read_bytes() == new:
                continue
            (changed if target.exists() else added).append(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".updating")
            tmp.write_bytes(new)
            mode = (info.external_attr >> 16) & 0o777
            if mode & 0o100:
                os.chmod(tmp, mode | 0o644)
            os.replace(tmp, target)
    # files that the previous version shipped but this one doesn't -> remove (never user files)
    removed = []
    manifest = root / MANIFEST_FILE
    if manifest.exists():
        for rel in manifest.read_text().splitlines():
            if rel and rel not in shipped and not _protected(rel) and ".." not in Path(rel).parts:
                f = root / rel
                if f.is_file():
                    f.unlink()
                    removed.append(rel)
    manifest.write_text("\n".join(sorted(shipped)) + "\n")
    (root / VERSION_FILE).write_text(sha + "\n")
    return {"changed": changed, "added": added, "removed": removed, "skipped": skipped, "version": sha}


def update(root: Path = ROOT, force: bool = False) -> dict:
    """Download and install the latest version if there is one."""
    if is_git_checkout(root) and not force:
        return {"updated": False, "reason": "This folder is a git checkout. Use `git pull` instead."}
    sha = latest_sha()
    if sha == current_sha(root) and not force:
        return {"updated": False, "reason": "Already up to date.", "version": sha}
    result = apply_zip(download_zip(sha), sha, root)
    result["updated"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--check" in argv:
        print(json.dumps(check(), indent=2))
        return 0
    try:
        r = update(force="--force" in argv)
    except Exception as e:
        print(f"  Couldn't check for updates ({e}). Starting the current version.")
        return 0
    if r.get("updated"):
        n = len(r["changed"]) + len(r["added"])
        print(f"  Updated to the latest version ({r['version'][:7]}, {n} file(s) changed).")
    elif "--quiet" not in argv:
        print(f"  {r['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
