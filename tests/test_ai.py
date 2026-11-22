import json
from types import SimpleNamespace as NS

import pytest

from tradebot.ai import ClaudeTrader, schema, to_targets
from tradebot.bot import Bot


def answer(**decisions):
    return {"market_view": "Mixed market.", "note_to_self": "Watch BTC.",
            "decisions": [{"coin": c, "action": a, "size_pct": sz, "confidence": "medium", "reason": f"{c} reason"}
                          for c, (a, sz) in decisions.items()]}


class FakeClient:
    """Mimics client.beta.messages.create and records the request."""

    def __init__(self, data, stop="end_turn"):
        self.data, self.stop, self.calls = data, stop, []
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        return NS(stop_reason=self.stop, model=kw["model"], usage=NS(input_tokens=4000, output_tokens=1500),
                  content=[NS(type="thinking", thinking=""), NS(type="text", text=json.dumps(self.data))])


def test_request_shape_and_cost(market):
    fake = FakeClient(answer(BTC=("buy", 40), ETH=("stay_out", 0)))
    frames = {c: market.candles(c, 600) for c in ["BTC", "ETH"]}
    data = ClaudeTrader(fake).decide(frames, {"total_value_usd": 100}, "normal", {})
    req = fake.calls[0]
    assert req["model"] == "claude-opus-5-5" and req["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in req["betas"]
    assert req["output_config"]["format"]["schema"] == schema(["BTC", "ETH"])
    assert "BTC" in req["messages"][0]["content"] and "rsi_14h" in req["messages"][0]["content"]
    assert data["cost"] == pytest.approx(4000 * 4e-6 + 1500 * 20e-6)


def test_refusal_and_cutoff_raise(market):
    frames = {"BTC": market.candles("BTC", 600)}
    for stop in ("refusal", "max_tokens"):
        with pytest.raises(RuntimeError):
            ClaudeTrader(FakeClient(answer(), stop=stop)).decide(frames, {}, "normal", {})


def test_guardrails():
    # normal = max 2 coins, 50% each
    t, w, d = to_targets(answer(BTC=("buy", 90), ETH=("buy", 30), SOL=("buy", 30)), ["BTC", "ETH", "SOL"], set(), "normal")
    assert t == {"BTC": 1, "ETH": 1, "SOL": 0}
    assert w == {"BTC": 0.5, "ETH": 0.3}
    assert {x["coin"]: x["action"] for x in d}["SOL"] == "stay_out"
    # held coins: buy -> hold, stay_out -> sell, missing -> hold
    t, w, d = to_targets(answer(BTC=("buy", 50), ETH=("stay_out", 0)), ["BTC", "ETH", "SOL"], {"BTC", "ETH", "SOL"}, "normal")
    assert t == {"BTC": 1, "ETH": 0, "SOL": 1}
    assert {x["coin"]: x["action"] for x in d} == {"BTC": "hold", "ETH": "sell", "SOL": "hold"}


class FakeTrader:
    def __init__(self, data):
        self.data, self._client = data, object()

    def decide(self, frames, account, risk, memory):
        self.seen = (account, memory)
        return {**self.data, "cost": 0.05}


def test_claude_bot_trades_and_remembers(market, tmp_path):
    trader = FakeTrader(answer(BTC=("buy", 40), ETH=("stay_out", 0), SOL=("stay_out", 0)))
    bot = Bot(market=market, path=tmp_path / "c.json", kind="claude", trader=trader)
    bot.tick(force_decision=True)
    assert set(bot.portfolio.positions) == {"BTC"}
    value = bot.portfolio.positions["BTC"] * market.prices(["BTC"])["BTC"]
    assert value == pytest.approx(40, rel=0.02)
    snap = bot.snapshot()
    assert snap["ai"]["cost"] == 0.05 and snap["ai"]["calls"] == 1 and snap["ai"]["view"] == "Mixed market."
    assert snap["views"]["BTC"]["reason"] == "BTC reason"
    assert any("Claude" in e["text"] for e in snap["events"])
    # next decision gets Claude's own note back
    trader.data = answer(BTC=("sell", 0), ETH=("stay_out", 0), SOL=("stay_out", 0))
    bot.tick(force_decision=True)
    assert trader.seen[1]["note"] == "Watch BTC." and trader.seen[0]["positions"][0]["coin"] == "BTC"
    assert bot.portfolio.positions == {}


def test_claude_respects_interval(market, tmp_path):
    trader = FakeTrader(answer(BTC=("stay_out", 0)))
    bot = Bot(market=market, path=tmp_path / "c.json", kind="claude", trader=trader)
    bot.update_settings(ai_every_hours=4)
    bot.tick(force_decision=True)
    bot.last_decision = None  # a new candle closed
    calls = bot.ai["calls"]
    bot.tick()
    assert bot.ai["calls"] == calls  # not due yet


def test_web_claude_rules(market, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from tradebot.web import create_app

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TRADEBOT_PASSWORD", raising=False)
    bots = {"autopilot": Bot(market=market, path=tmp_path / "a.json"),
            "claude": Bot(market=market, path=tmp_path / "c.json", kind="claude")}
    c = TestClient(create_app(bots))
    s = c.get("/api/state?bot=claude").json()
    assert s["kind"] == "claude" and s["ai"]["configured"] is False and set(s["bots"]) == {"autopilot", "claude"}
    assert "API key" in c.post("/api/start?bot=claude").json()["error"]
    assert "remember" in c.post("/api/test?bot=claude", json={"days": 30}).json()["error"]
    assert c.post("/api/settings?bot=claude", json={"ai_every_hours": 3}).status_code == 400
    assert c.get("/api/state?bot=nope").status_code == 400
