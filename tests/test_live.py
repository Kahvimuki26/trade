"""Real-money mode against a fake exchange: no network, no keys."""

import math

import pytest

from tradebot.bot import Bot
from tradebot.live import LivePortfolio, live_config


class FakeExchange:
    id = "fakex"

    def __init__(self, prices, quote_balance=100.0, min_cost=5.0, fee_in_base=True):
        self.prices, self.min = prices, min_cost
        self.bal = {"USDC": quote_balance}
        self.fee_in_base = fee_in_base
        self.orders = []
        self.markets = {f"{c}/USDC": {"limits": {"cost": {"min": min_cost}}} for c in prices}

    def market(self, pair):
        return self.markets[pair]

    def fetch_tickers(self, pairs):
        return {p: {"last": self.prices[p.split("/")[0]]} for p in pairs}

    def fetch_balance(self):
        return {k: {"free": v, "total": v} for k, v in self.bal.items()}

    def amount_to_precision(self, pair, amount):
        return f"{math.floor(amount * 1e6) / 1e6:.6f}"  # ccxt truncates by default

    def _order(self, coin, side, amount, cost):
        o = {"id": str(len(self.orders)), "symbol": f"{coin}/USDC", "status": "closed", "filled": amount,
             "cost": cost, "average": cost / amount}
        fee = amount * 0.001
        o["fee"] = {"currency": coin, "cost": fee} if (side == "buy" and self.fee_in_base) else \
            {"currency": "USDC", "cost": cost * 0.001}
        self.orders.append((side, coin, amount, cost))
        return o

    def create_market_buy_order_with_cost(self, pair, cost):
        coin = pair.split("/")[0]
        amount = cost / self.prices[coin]
        assert cost <= self.bal["USDC"] + 1e-9, "spent money that isn't there"
        self.bal["USDC"] -= cost
        o = self._order(coin, "buy", amount, cost)
        self.bal[coin] = self.bal.get(coin, 0) + amount - (o["fee"]["cost"] if o["fee"]["currency"] == coin else 0)
        return o

    def create_market_sell_order(self, pair, amount):
        coin = pair.split("/")[0]
        assert amount <= self.bal.get(coin, 0) + 1e-9, "sold coins the bot doesn't have"
        cost = amount * self.prices[coin]
        self.bal[coin] -= amount
        o = self._order(coin, "sell", amount, cost)
        self.bal["USDC"] += cost - o["fee"]["cost"]
        return o

    def fetch_order(self, oid, symbol=None):
        raise AssertionError("orders fill instantly in this fake")


CFG = {"exchange": "fakex", "key": "k", "secret": "s", "password": None, "quote": "USDC", "budget": 15.0,
       "max_loss": 0.3, "testnet": False}


def test_off_unless_enabled(monkeypatch):
    monkeypatch.delenv("LIVE_TRADING", raising=False)
    assert live_config() is None
    monkeypatch.setenv("LIVE_TRADING", "yes")
    monkeypatch.setenv("LIVE_BUDGET", "15")
    assert live_config()["budget"] == 15 and live_config()["quote"] == "USDC"


def test_budget_cap_fees_and_own_coins_only():
    ex = FakeExchange({"BTC": 50_000.0, "ETH": 3_000.0}, quote_balance=100.0)
    ex.bal["ETH"] = 2.0  # the user's own ETH: must never be touched
    pf = LivePortfolio(ex, "USDC", 15.0)
    pf.rebalance({"BTC": 1}, {"BTC": 50_000.0}, "t", weights={"BTC": 1.0})
    side, coin, amount, cost = ex.orders[0]
    assert cost == pytest.approx(15.0)  # budget, not the $100 on the account
    assert pf.positions["BTC"] == pytest.approx(amount * 0.999)  # fee paid in BTC
    assert pf.cash == pytest.approx(0.0)
    ex.prices["BTC"] = 55_000.0
    pf.rebalance({"BTC": 0, "ETH": 0}, {"BTC": 55_000.0, "ETH": 3000.0}, "t2", weights={})
    assert pf.positions == {} and ex.bal["ETH"] == 2.0
    # ~+10% minus fees (amounts are truncated to the exchange's precision, so allow a little slack)
    assert pf.trades[-1].realized_pnl == pytest.approx(15 * 1.1 * 0.999 * 0.999 - 15, rel=0.01)
    assert pf.cash == pytest.approx(ex.bal["USDC"] - 85)  # exactly what the sale put back in the account


def test_below_minimum_is_skipped():
    ex = FakeExchange({"BTC": 50_000.0}, min_cost=5.0)
    pf = LivePortfolio(ex, "USDC", 15.0)
    pf.rebalance({"BTC": 1}, {"BTC": 50_000.0}, "t", weights={"BTC": 0.2})  # $3 < $5 minimum
    assert ex.orders == [] and "minimum" in pf.notes[0]


def test_reconcile_after_manual_sell():
    ex = FakeExchange({"BTC": 50_000.0})
    pf = LivePortfolio(ex, "USDC", 15.0)
    pf.rebalance({"BTC": 1}, {"BTC": 50_000.0}, "t", weights={"BTC": 1.0})
    ex.bal["BTC"] = 0.0  # user sold it on the exchange website
    pf.reconcile()
    assert pf.positions == {}


def live_bot(market, tmp_path, ex):
    class Trader:
        _client = object()

        def decide(self, frames, account, risk, memory, news=False, live=False):
            assert live is True
            return {"market_view": "ok", "note_to_self": "", "cost": 0.3, "news": "n",
                    "decisions": [{"coin": c, "action": "buy" if c == "BTC" else "stay_out", "size_pct": 50,
                                   "confidence": "medium", "reason": "r"} for c in frames]}

    bot = Bot(market=market, path=tmp_path / "live.json", trader=Trader(), live=CFG, exchange=ex)
    bot.update_settings(coins=["BTC", "ETH"])
    return bot


def test_live_bot_trades_real_orders_and_panics(market, tmp_path):
    ex = FakeExchange({c: market.prices([c])[c] for c in ["BTC", "ETH"]}, quote_balance=40)
    bot = live_bot(market, tmp_path, ex)
    assert bot.kind == "live" and bot.settings["cash"] == 15
    bot.tick(force_decision=True)
    assert [o[0] for o in ex.orders] == ["buy"] and ex.orders[0][3] == pytest.approx(7.5)
    assert bot.snapshot()["live"]["budget"] == 15 and "key" not in bot.snapshot()["live"]
    assert bot.panic() == 1 and bot.portfolio.positions == {} and not bot.running


def test_loss_limit_sells_and_stops(market, tmp_path):
    ex = FakeExchange({c: market.prices([c])[c] for c in ["BTC", "ETH"]}, quote_balance=40)
    bot = live_bot(market, tmp_path, ex)
    bot.update_settings(risk="aggressive")
    bot.tick(force_decision=True)  # all-in BTC
    ex.prices["BTC"] *= 0.5  # crash: 15 -> ~7.5, below the 30% loss limit (but stop-loss is 8%, so it sells first)
    bot.tick()
    assert bot.portfolio.positions == {}
    assert any("Stop-loss" in e["text"] or "LOSS LIMIT" in e["text"] for e in bot.events)


def test_web_live_requires_confirmation(market, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from tradebot.web import create_app

    monkeypatch.delenv("TRADEBOT_PASSWORD", raising=False)
    ex = FakeExchange({c: market.prices([c])[c] for c in ["BTC", "ETH"]})
    c = TestClient(create_app({"live": live_bot(market, tmp_path, ex)}))
    assert c.post("/api/start?bot=live").status_code == 400
    assert c.post("/api/start?bot=live", json={"confirm": "REAL"}).json()["running"]
    c.post("/api/panic?bot=live")
    bot = c.app.state.bots["live"]
    assert bot.portfolio.positions == {} and not bot.running


def test_coin_amount_minimum():
    ex = FakeExchange({"BTC": 50_000.0}, min_cost=0.5)
    ex.markets["BTC/USDC"]["limits"]["amount"] = {"min": 0.0002}  # = $10 at this price
    pf = LivePortfolio(ex, "USDC", 15.0)
    pf.rebalance({"BTC": 1}, {"BTC": 50_000.0}, "t", weights={"BTC": 0.5})  # $7.50 < $10
    assert ex.orders == [] and "minimum" in pf.notes[0]
