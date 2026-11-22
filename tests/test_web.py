import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tradebot.config import load_config  # noqa: E402
from tradebot.paper import Runner  # noqa: E402
from tradebot.web import create_app  # noqa: E402

from .conftest import make_bars  # noqa: E402
from .test_paper import FakeProvider  # noqa: E402


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADEBOT_PASSWORD", raising=False)
    cfg = tmp_path / "instances.yaml"
    cfg.write_text(f"""
defaults:
  data_source: fake
  state_dir: {tmp_path / 'state'}
instances:
  - name: hold-spy
    strategy: buy_and_hold
    symbols: [SPY]
""")
    data = tmp_path / "data"
    data.mkdir()
    for i, s in enumerate(["SPY", "QQQ"]):
        make_bars(n=600, seed=i).to_csv(data / f"{s}.csv", index_label="date")
    runner = Runner(lambda: load_config(cfg), providers={"fake": FakeProvider()})
    client = TestClient(create_app(cfg, runner=runner))
    return client, cfg, data


def test_index_and_strategies(setup):
    client, _, _ = setup
    assert "tradebot" in client.get("/").text
    names = [s["name"] for s in client.get("/api/strategies").json()]
    assert "opening_candle" in names and "sma_cross" in names


def test_overview_and_forced_tick(setup):
    client, _, _ = setup
    ov = client.get("/api/overview").json()
    assert ov["instances"][0]["started"] is False and ov["runner"]["running"] is False
    assert client.post("/api/runner/tick", json={"force": True}).json()["updated"] == 1
    inst = client.get("/api/instances/hold-spy").json()
    assert inst["started"] and inst["positions"]["SPY"] > 0 and len(inst["trades"]) == 1
    assert client.get("/api/history").json()[0]["name"] == "hold-spy"
    assert any("BUY" in line["msg"] for line in client.get("/api/logs").json())


def test_add_edit_toggle_delete_instance(setup):
    client, cfg, _ = setup
    r = client.post("/api/instances", json={"name": "sma-qqq", "strategy": "sma_cross", "symbols": "qqq, spy",
                                            "params": {"fast": 10, "slow": 50}})
    assert r.status_code == 200, r.text
    inst = load_config(cfg).get("sma-qqq")
    assert inst.symbols == ["QQQ", "SPY"] and inst.params == {"fast": 10}  # defaults not written
    assert client.post("/api/instances", json={"name": "sma-qqq", "strategy": "sma_cross"}).status_code == 400

    client.post("/api/instances", json={"original_name": "sma-qqq", "name": "sma-qqq2", "strategy": "sma_cross",
                                        "symbols": "QQQ"})
    assert [i.name for i in load_config(cfg).instances] == ["hold-spy", "sma-qqq2"]
    client.post("/api/instances/sma-qqq2/enabled", json={"enabled": False})
    assert load_config(cfg).get("sma-qqq2").enabled is False
    client.delete("/api/instances/sma-qqq2")
    assert [i.name for i in load_config(cfg).instances] == ["hold-spy"]
    assert cfg.with_suffix(".yaml.bak").exists()


def test_invalid_config_rejected(setup):
    client, cfg, _ = setup
    before = cfg.read_text()
    r = client.put("/api/config", json={"text": "instances:\n  - {name: x, strategy: nope, symbols: [SPY]}\n"})
    assert r.status_code == 400 and "Unknown strategy" in r.json()["error"]
    assert cfg.read_text() == before


def test_backtest_and_compare(setup):
    client, _, data = setup
    r = client.post("/api/backtest", json={"strategy": "sma_cross", "symbols": "SPY", "source": "csv",
                                           "csv_dir": str(data), "start": "2020-01-01", "params": {"fast": 10}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["metrics"]["orders"] > 0 and len(body["equity"]) > 100 and "fast=10" in body["title"]
    r = client.post("/api/compare", json={"symbols": "SPY", "source": "csv", "csv_dir": str(data),
                                          "start": "2020-01-01"})
    rows = r.json()
    assert {row["strategy"] for row in rows["rows"]} >= {"buy_and_hold", "sma_cross"}
    assert "opening_candle" in rows["skipped"]


def test_password_and_cloudflare_guard(setup, monkeypatch, tmp_path):
    client, cfg, _ = setup
    # no password: direct access fine, but via Cloudflare refused
    assert client.get("/api/overview").status_code == 200
    assert client.get("/api/overview", headers={"cf-connecting-ip": "1.2.3.4"}).status_code == 403

    monkeypatch.setenv("TRADEBOT_PASSWORD", "hunter2")
    locked = TestClient(create_app(cfg, runner=Runner(lambda: load_config(cfg))))
    assert locked.get("/api/overview").status_code == 401
    assert locked.get("/api/overview", auth=("me", "wrong")).status_code == 401
    assert locked.get("/api/overview", auth=("me", "hunter2"),
                      headers={"cf-connecting-ip": "1.2.3.4"}).status_code == 200


def test_runner_background_start_stop(setup):
    client, _, _ = setup
    client.post("/api/runner/start")
    assert client.get("/api/overview").json()["runner"]["running"] is True
    runner = client.app.state.runner
    runner.stop()
    assert runner.running is False
