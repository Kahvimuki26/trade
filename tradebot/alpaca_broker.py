"""Send an instance's orders to an Alpaca *paper* account.

Orders show up in the Alpaca dashboard / app and are filled by Alpaca's paper
engine at real market prices. The client is created with ``paper=True``, so it
can't touch a live-money account.

Alpaca gives one paper account per set of API keys, so each instance using
``broker: alpaca`` needs its own account (``alpaca_account`` in the config):

    alpaca_account: default  ->  ALPACA_API_KEY / ALPACA_SECRET_KEY
    alpaca_account: swing    ->  ALPACA_SWING_API_KEY / ALPACA_SWING_SECRET_KEY
"""

from __future__ import annotations

import logging
import math
import os
import time
from typing import Any

from .portfolio import Trade

log = logging.getLogger("tradebot")

_FINAL = {"filled", "canceled", "expired", "rejected", "done_for_day", "stopped", "suspended"}


def credentials(account: str = "default") -> tuple[str, str]:
    prefix = "ALPACA" if account in ("", "default") else f"ALPACA_{account.upper()}"
    key, secret = os.environ.get(f"{prefix}_API_KEY"), os.environ.get(f"{prefix}_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError(f"Alpaca account '{account}' needs {prefix}_API_KEY and {prefix}_SECRET_KEY "
                           "in your environment or .env file")
    return key, secret


def _val(x: Any) -> str:
    return str(getattr(x, "value", x)).lower()


class AlpacaPaperBroker:
    def __init__(self, account: str = "default", fractional: bool = True, client: Any = None,
                 fill_timeout: float = 20.0):
        if client is None:
            try:
                from alpaca.trading.client import TradingClient
            except ImportError as e:  # pragma: no cover
                raise ImportError("Install alpaca-py:  pip install -e '.[alpaca]'") from e
            client = TradingClient(*credentials(account), paper=True)
        self.client = client
        self.account = account
        self.fractional = fractional
        self.fill_timeout = fill_timeout
        self.trades: list[Trade] = []

    # ------------------------------------------------------------------ state
    def _positions_raw(self) -> dict[str, Any]:
        return {p.symbol: p for p in self.client.get_all_positions()}

    @property
    def positions(self) -> dict[str, float]:
        out = {}
        for sym, p in self._positions_raw().items():
            q = abs(float(p.qty))
            out[sym] = -q if _val(p.side) == "short" else q
        return out

    def direction(self, symbol: str) -> int:
        q = self.positions.get(symbol, 0.0)
        return 1 if q > 0 else -1 if q < 0 else 0

    def current_equity(self, prices: dict[str, float] | None = None) -> float:
        return float(self.client.get_account().equity)

    # ------------------------------------------------------------------ orders
    def _wait(self, order: Any) -> Any:
        deadline = time.monotonic() + self.fill_timeout
        while _val(order.status) not in _FINAL and time.monotonic() < deadline:
            time.sleep(0.5)
            order = self.client.get_order_by_id(order.id)
        return order

    def _record(self, symbol: str, qty: float, order: Any, ref_price: float, ts: Any,
                entry: float | None = None) -> Trade:
        status = _val(order.status)
        if status != "filled":
            log.warning("[alpaca:%s] %s order for %s is '%s' (not filled yet)", self.account,
                        "BUY" if qty > 0 else "SELL", symbol, status)
        filled_qty = float(order.filled_qty or 0) or abs(qty)
        price = float(order.filled_avg_price) if order.filled_avg_price else ref_price
        signed = math.copysign(filled_qty, qty)
        pnl = None
        if entry is not None:
            pnl = (price - entry) * filled_qty * (1 if qty < 0 else -1)
        t = Trade(str(ts), symbol, signed, price, 0.0, pnl)
        self.trades.append(t)
        return t

    def rebalance(self, targets: dict[str, int], prices: dict[str, float], ts: Any,
                  allocation: float = 1.0, weights: dict[str, float] | None = None,
                  max_leverage: float = 1.0) -> list[Trade]:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        weights = weights or {s: 1 / len(targets) for s in targets}
        raw = self._positions_raw()
        pos = self.positions
        cur_dir = {s: (1 if pos.get(s, 0) > 0 else -1 if pos.get(s, 0) < 0 else 0) for s in targets}
        changing = [s for s, d in targets.items() if d != cur_dir[s]]
        if not changing:
            return []
        trades: list[Trade] = []

        for s in changing:  # 1) close
            if pos.get(s):
                entry = float(raw[s].avg_entry_price)
                order = self._wait(self.client.close_position(s))
                trades.append(self._record(s, -pos[s], order, prices[s], ts, entry))

        acct = self.client.get_account()
        equity = float(acct.equity)
        gross = sum(abs(float(p.market_value)) for p in self.client.get_all_positions())
        buying_power = float(acct.buying_power)

        for s in changing:  # 2) open
            d = targets[s]
            if d == 0:
                continue
            budget = min(equity * allocation * weights.get(s, 0), max_leverage * equity - gross, buying_power)
            price = prices[s]
            if budget <= price * 0.01:
                continue
            if d > 0 and self.fractional:
                req = MarketOrderRequest(symbol=s, notional=round(budget, 2), side=OrderSide.BUY,
                                         time_in_force=TimeInForce.DAY)
                qty = budget / price
            else:  # Alpaca doesn't allow fractional shorts
                qty = math.floor(budget / price)
                if qty < 1:
                    continue
                req = MarketOrderRequest(symbol=s, qty=qty, side=OrderSide.BUY if d > 0 else OrderSide.SELL,
                                         time_in_force=TimeInForce.DAY)
            try:
                order = self._wait(self.client.submit_order(req))
            except Exception as e:
                log.error("[alpaca:%s] order for %s rejected: %s", self.account, s, e)
                continue
            trades.append(self._record(s, d * qty, order, price, ts))
            gross += budget
            buying_power -= budget
        return trades
