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

    def decide(self, frames, account, risk, memory, news=False, live=False):
        self.seen = (account, memory)
        self.flags = (news, live)
        return {**self.data, "cost": 0.05, "news": "Big news." if news else None}


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


def test_news_call_then_decision(market):
    class TwoStep(FakeClient):
        def create(self, **kw):
            self.calls.append(kw)
            if kw.get("tools"):  # the news call
                stop = "pause_turn" if len(self.calls) == 1 else "end_turn"
                return NS(stop_reason=stop, model=kw["model"],
                          usage=NS(input_tokens=20000, output_tokens=800, server_tool_use=NS(web_search_requests=2)),
                          content=[NS(type="text", text="BTC: ETF inflows (Nov 21).")])
            return super().create(**kw)

    fake = TwoStep(answer(BTC=("hold", 0)))
    data = ClaudeTrader(fake).decide({"BTC": market.candles("BTC", 600)}, {}, "normal", {}, news=True)
    news_calls = [c for c in fake.calls if c.get("tools")]
    assert len(news_calls) == 2  # paused once, resumed
    assert news_calls[0]["tools"][0]["type"] == "web_search_20260209"
    assert news_calls[1]["messages"][-1]["role"] == "assistant"  # resume = resend the paused turn
    decision = [c for c in fake.calls if not c.get("tools")][0]
    assert decision["output_config"]["effort"] == "high" and "ETF inflows" in decision["messages"][0]["content"]
    assert data["searches"] == 4 and data["news"].startswith("BTC")
    assert data["cost"] == pytest.approx(2 * (20000 * 4e-6 + 800 * 20e-6 + 0.02) + 4000 * 4e-6 + 1500 * 20e-6)


def test_daily_budget_stops_decisions(market, tmp_path):
    trader = FakeTrader(answer(BTC=("stay_out", 0)))
    bot = Bot(market=market, path=tmp_path / "c.json", kind="claude", trader=trader)
    bot.update_settings(ai_daily_budget=0.5, ai_every_hours=1)
    for _ in range(15):
        bot.last_decision, bot.ai["last"] = None, None
        bot.tick()
    assert bot.ai["calls"] == 10  # 10 x $0.05 = $0.50, then it stops for the day
    assert any("daily budget" in e["text"] for e in bot.events)
    assert trader.flags == (True, False)  # news on by default
