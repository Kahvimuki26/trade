import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tradebot.bot import Bot  # noqa: E402
from tradebot.web import create_app, pick_port  # noqa: E402


@pytest.fixture
def client(market, tmp_path, monkeypatch):
    monkeypatch.delenv("TRADEBOT_PASSWORD", raising=False)
    return TestClient(create_app(Bot(market=market, path=tmp_path / "bot.json")))


def test_page_and_state(client):
    assert "autopilot" in client.get("/").text
    s = client.get("/api/state").json()
    assert s["balance"] == 100 and s["settings"]["risk"] == "normal" and "BTC" in s["coins_available"]
    assert set(s["risk_levels"]) == {"safe", "normal", "aggressive"}


def test_settings_reset_and_validation(client):
    assert client.post("/api/settings", json={"risk": "safe", "coins": ["btc", "doge"]}).status_code == 200
    s = client.get("/api/state").json()["settings"]
    assert s["risk"] == "safe" and s["coins"] == ["BTC", "DOGE"]
    assert client.post("/api/settings", json={"coins": []}).status_code == 400
    assert client.post("/api/settings", json={"risk": "yolo"}).status_code == 400
    assert client.post("/api/reset", json={"cash": 250}).status_code == 200
    assert client.get("/api/state").json()["balance"] == 250
    assert client.post("/api/reset", json={"cash": 0.5}).status_code == 400


def test_test_on_past(client):
    r = client.post("/api/test", json={"days": 30}).json()
    assert r["days"] == 30 and r["curve"] and "hold_return" in r


def test_start_and_stop(client):
    assert client.post("/api/start").json()["running"]
    client.app.state.bot.stop()
    assert not client.get("/api/state").json()["running"]


def test_password_and_cloudflare_guard(market, tmp_path, monkeypatch, client):
    assert client.get("/api/state", headers={"cf-ray": "x"}).status_code == 403
    monkeypatch.setenv("TRADEBOT_PASSWORD", "pw")
    locked = TestClient(create_app(Bot(market=market, path=tmp_path / "b2.json")))
    assert locked.get("/api/state").status_code == 401
    assert locked.get("/api/state", auth=("x", "pw"), headers={"cf-ray": "x"}).status_code == 200


def test_pick_port_skips_busy_port():
    import socket

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        assert pick_port("127.0.0.1", taken) != taken
