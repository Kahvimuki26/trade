"""AlpacaPaperBroker against a fake Alpaca client (no network, no keys)."""

from types import SimpleNamespace as NS

import pytest

from tradebot.alpaca_broker import AlpacaPaperBroker, credentials
from tradebot.config import load_config


class FakeAlpaca:
    def __init__(self, cash=10_000.0, prices=None):
        self.cash = cash
        self.prices = prices or {"SPY": 500.0, "QQQ": 400.0}
        self.pos: dict[str, list] = {}  # sym -> [qty, avg]
        self.orders = []

    def _fill(self, sym, qty):
        price = self.prices[sym]
        q, avg = self.pos.get(sym, [0.0, 0.0])
        new = q + qty
        avg = price if (q == 0 or (q > 0) != (new > 0)) else (avg if abs(new) < abs(q) else (avg * q + price * qty) / new)
        self.cash -= qty * price
        if abs(new) < 1e-9:
            self.pos.pop(sym, None)
        else:
            self.pos[sym] = [new, avg]
        o = NS(id=str(len(self.orders)), status="filled", filled_avg_price=str(price), filled_qty=str(abs(qty)))
        self.orders.append(o)
        return o

    def get_account(self):
        eq = self.cash + sum(q * self.prices[s] for s, (q, _) in self.pos.items())
        return NS(equity=str(eq), cash=str(self.cash), buying_power=str(eq * 2))

    def get_all_positions(self):
        return [NS(symbol=s, qty=str(abs(q)), side="short" if q < 0 else "long", avg_entry_price=str(a),
                   market_value=str(q * self.prices[s])) for s, (q, a) in self.pos.items()]

    def close_position(self, sym):
        return self._fill(sym, -self.pos[sym][0])

    def submit_order(self, req):
        side = 1 if "buy" in str(getattr(req.side, "value", req.side)).lower() else -1
        qty = req.qty if req.qty else req.notional / self.prices[req.symbol]
        return self._fill(req.symbol, side * float(qty))

    def get_order_by_id(self, oid):
        return self.orders[int(oid)]


def test_open_flip_close():
    pytest.importorskip("alpaca")
    fake = FakeAlpaca()
    b = AlpacaPaperBroker(client=fake)
    b.rebalance({"SPY": 1}, {"SPY": 500.0}, "t0", weights={"SPY": 0.5})
    assert b.positions["SPY"] == pytest.approx(10)  # $5000 notional
    assert b.rebalance({"SPY": 1}, {"SPY": 500.0}, "t1") == []  # idempotent

    fake.prices["SPY"] = 550.0
    trades = b.rebalance({"SPY": -1}, {"SPY": 550.0}, "t2", weights={"SPY": 0.5})
    assert trades[0].realized_pnl == pytest.approx(500)
    assert b.positions["SPY"] == -9  # shorts are whole shares: floor(5250/550)
    b.rebalance({"SPY": 0}, {"SPY": 550.0}, "t3")
    assert b.positions == {} and len(b.trades) == 4


def test_leverage_cap():
    pytest.importorskip("alpaca")
    b = AlpacaPaperBroker(client=FakeAlpaca())
    b.rebalance({"SPY": 1, "QQQ": 1}, {"SPY": 500.0, "QQQ": 400.0}, "t",
                weights={"SPY": 0.8, "QQQ": 0.8}, max_leverage=1.0)
    gross = sum(abs(float(p.market_value)) for p in b.client.get_all_positions())
    assert gross == pytest.approx(10_000)  # second order trimmed to stay at 1x


def test_credentials(monkeypatch):
    monkeypatch.setenv("ALPACA_SWING_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SWING_SECRET_KEY", "s")
    assert credentials("swing") == ("k", "s")
    with pytest.raises(RuntimeError, match="ALPACA_NOPE_API_KEY"):
        credentials("nope")


def test_config_rejects_shared_alpaca_account(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("""
instances:
  - {name: a, strategy: sma_cross, symbols: [SPY], broker: alpaca}
  - {name: b, strategy: rsi_reversion, symbols: [SPY], broker: alpaca}
""")
    with pytest.raises(ValueError, match="own paper account"):
        load_config(p)


def test_config_strategy_requirements(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("""
defaults: {interval: 1d, max_leverage: 1}
instances:
  - {name: a, strategy: opening_candle}
  - {name: b, strategy: opening_candle, symbols: [IWM], max_leverage: 1.5}
  - {name: c, strategy: sma_cross, symbols: [SPY]}
""")
    a, b, c = load_config(p).instances
    assert (a.interval, a.symbols, a.max_leverage) == ("5m", ["SPY", "QQQ"], 2.0)
    assert (b.symbols, b.max_leverage) == (["IWM"], 1.5)
    assert (c.interval, c.max_leverage) == ("1d", 1)
