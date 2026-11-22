"""Simulated account shared by the backtester and the paper trader.

Using the same accounting for both means a strategy behaves the same in a
backtest and in paper trading.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Trade:
    ts: str
    symbol: str
    qty: float  # signed: + buy, - sell
    price: float  # fill price incl. slippage
    fee: float
    realized_pnl: float | None = None  # set when the trade reduces / closes a position

    @property
    def side(self) -> str:
        return "BUY" if self.qty > 0 else "SELL"


@dataclass
class Portfolio:
    cash: float
    commission: float = 0.0  # flat fee per order
    slippage_bps: float = 1.0  # adverse price move per fill, in basis points
    fractional: bool = True  # allow fractional shares
    positions: dict[str, float] = field(default_factory=dict)
    avg_price: dict[str, float] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)

    # ------------------------------------------------------------------ state
    def qty(self, symbol: str) -> float:
        return self.positions.get(symbol, 0.0)

    def direction(self, symbol: str) -> int:
        q = self.qty(symbol)
        return 1 if q > 0 else -1 if q < 0 else 0

    def equity(self, prices: dict[str, float]) -> float:
        return float(self.cash + sum(q * prices[s] for s, q in self.positions.items() if q))

    # ------------------------------------------------------------------ orders
    def order(self, symbol: str, qty: float, price: float, ts: Any) -> Trade | None:
        """Fill a market order immediately at ``price`` +/- slippage."""
        qty, price = float(qty), float(price)
        if qty == 0 or not math.isfinite(qty):
            return None
        sign = 1 if qty > 0 else -1
        fill = price * (1 + sign * self.slippage_bps / 10_000)
        fee = self.commission
        cur = self.qty(symbol)
        realized = None

        if cur != 0 and (cur > 0) != (qty > 0):  # reducing or flipping
            closed = min(abs(qty), abs(cur))
            direction = 1 if cur > 0 else -1
            realized = (fill - self.avg_price[symbol]) * closed * direction - fee

        new = cur + qty
        if abs(new) < 1e-12:
            new = 0.0
        if new == 0:
            self.avg_price.pop(symbol, None)
        elif cur == 0 or (cur > 0) != (new > 0):  # fresh or flipped position
            self.avg_price[symbol] = fill
        elif (cur > 0) == (qty > 0):  # adding to position
            self.avg_price[symbol] = (self.avg_price[symbol] * cur + fill * qty) / new

        if new == 0:
            self.positions.pop(symbol, None)
        else:
            self.positions[symbol] = new
        self.cash -= qty * fill + fee
        trade = Trade(str(ts), symbol, qty, fill, fee, realized)
        self.trades.append(trade)
        return trade

    def rebalance(
        self,
        targets: dict[str, int],
        prices: dict[str, float],
        ts: Any,
        allocation: float = 1.0,
        weights: dict[str, float] | None = None,
    ) -> list[Trade]:
        """Move each symbol to its target direction (-1/0/1).

        Only trades when the direction changes, so calling it repeatedly with
        the same targets is a no-op. Closes go before opens so freed cash can
        be reused. A new position gets ``equity * allocation * weight``.
        """
        weights = weights or {s: 1 / len(targets) for s in targets}
        trades: list[Trade] = []
        changing = [s for s, d in targets.items() if d != self.direction(s)]

        for s in changing:  # 1) close / flip-close
            if self.qty(s):
                t = self.order(s, -self.qty(s), prices[s], ts)
                trades.append(t)

        all_prices = {**prices}
        for s in self.positions:
            all_prices.setdefault(s, self.avg_price[s])
        equity = self.equity(all_prices)

        for s in changing:  # 2) open
            d = targets[s]
            if d == 0:
                continue
            budget = equity * allocation * weights.get(s, 0) - self.commission
            if d > 0:
                budget = min(budget, self.cash - self.commission)
            if budget <= 0:
                continue
            fill = prices[s] * (1 + self.slippage_bps / 10_000)
            qty = budget / fill
            if not self.fractional:
                qty = math.floor(qty)
            if qty > 0:
                trades.append(self.order(s, d * qty, prices[s], ts))
        return [t for t in trades if t is not None]

    # ------------------------------------------------------------- persistence
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Portfolio":
        d = dict(d)
        d["trades"] = [Trade(**t) for t in d.get("trades", [])]
        return cls(**d)
