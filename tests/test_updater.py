import io
import zipfile

import pytest

from tradebot import updater


def make_zip(files: dict[str, str], top="trade-abc123") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{top}/", "")
        for name, content in files.items():
            z.writestr(f"{top}/{name}", content)
    return buf.getvalue()


@pytest.fixture
def project(tmp_path):
    (tmp_path / "tradebot").mkdir()
    (tmp_path / "tradebot" / "old.py").write_text("old")
    (tmp_path / "tradebot" / "same.py").write_text("same")
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "instances.yaml").write_text("MY CONFIG")
    (tmp_path / ".env").write_text("ALPACA_API_KEY=mine")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "a.json").write_text("{}")
    (tmp_path / "tradebot" / "my_own_strategy.py").write_text("mine")
    return tmp_path


def test_apply_keeps_user_files(project):
    data = make_zip({
        "tradebot/old.py": "new", "tradebot/same.py": "same", "tradebot/added.py": "added",
        "configs/instances.yaml": "UPSTREAM CONFIG", ".env": "nope", "state/a.json": "nope",
        "../evil.py": "nope",
    })
    r = updater.apply_zip(data, "abc123", project)
    assert (project / "tradebot" / "old.py").read_text() == "new"
    assert (project / "tradebot" / "added.py").read_text() == "added"
    assert (project / "configs" / "instances.yaml").read_text() == "MY CONFIG"
    assert (project / ".env").read_text() == "ALPACA_API_KEY=mine"
    assert (project / "state" / "a.json").read_text() == "{}"
    assert (project / "tradebot" / "my_own_strategy.py").read_text() == "mine"
    assert not (project.parent / "evil.py").exists()
    assert r["changed"] == ["tradebot/old.py"] and r["added"] == ["tradebot/added.py"]
    assert updater.current_sha(project) == "abc123"


def test_update_flow(project, monkeypatch):
    monkeypatch.setattr(updater, "latest_sha", lambda: "v2")
    monkeypatch.setattr(updater, "download_zip", lambda sha: make_zip({"tradebot/old.py": sha}))
    assert updater.check(project)["available"] is True
    r = updater.update(project)
    assert r["updated"] and (project / "tradebot" / "old.py").read_text() == "v2"
    assert updater.update(project)["updated"] is False  # already current
    assert updater.check(project)["available"] is False


def test_refuses_git_checkout(project, monkeypatch):
    (project / ".git").mkdir()
    monkeypatch.setattr(updater, "latest_sha", lambda: pytest.fail("should not hit the network"))
    assert updater.update(project)["updated"] is False


def test_check_offline(project, monkeypatch):
    def boom():
        raise OSError("no internet")

    monkeypatch.setattr(updater, "latest_sha", boom)
    info = updater.check(project)
    assert info["available"] is False and "no internet" in info["error"]


def test_manifest_removes_files_dropped_upstream(project):
    updater.apply_zip(make_zip({"tradebot/a.py": "1", "tradebot/b.py": "1"}), "v1", project)
    (project / "tradebot" / "mine.py").write_text("user file")
    r = updater.apply_zip(make_zip({"tradebot/a.py": "2"}), "v2", project)
    assert r["removed"] == ["tradebot/b.py"]
    assert not (project / "tradebot" / "b.py").exists()
    assert (project / "tradebot" / "mine.py").exists() and (project / ".env").exists()


def test_legacy_cleanup(tmp_path):
    from tradebot import launch

    for rel in launch.LEGACY_FILES[:4] + ["tradebot/strategies/social/my_own.py"]:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    (tmp_path / "tradebot" / "strategies" / "base.py").write_text("x")
    (tmp_path / ".env").write_text("keep")
    n = launch.remove_legacy(tmp_path)
    assert n == 5
    assert (tmp_path / "tradebot" / "strategies" / "social" / "my_own.py").exists()  # user file kept
    assert (tmp_path / ".env").read_text() == "keep"
    assert not (tmp_path / "tradebot" / "backtest.py").exists()
