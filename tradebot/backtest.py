"""Event-driven backtester.

Timeline per bar ``t``:
  1. At the open of ``t``, fill orders for the targets decided at the close of ``t-1``.
  2. At the close of ``t``, mark equity and compute the new targets.

So a strategy can never trade on a price it hasn't seen yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .metrics import compute_metrics
from .portfolio import Portfolio, Trade
from .strategies import Strategy


@dataclass
class BacktestResult:
    strategy: Strategy
    symbols: list[str]
    equity: pd.Series
    trades: list[Trade]
    signals: dict[str, pd.Series]
    metrics: dict = field(default_factory=dict)

    def trades_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            [{**t.__dict__, "side": t.side} for t in self.trades],
            columns=["ts", "symbol", "side", "qty", "price", "fee", "realized_pnl"],
        )


def run_backtest(
    strategy: Strategy,
    data: dict[str, pd.DataFrame],
    cash: float = 10_000.0,
    commission: float = 0.0,
    slippage_bps: float = 1.0,
    fractional: bool = True,
    allocation: float = 1.0,
) -> BacktestResult:
    if not data:
        raise ValueError("No data to backtest")
    symbols = list(data)
    signals = {s: strategy.signals(df) for s, df in data.items()}

    index = pd.DatetimeIndex(sorted(set().union(*(df.index for df in data.values()))))
    opens = {s: data[s]["open"].reindex(index).to_numpy() for s in symbols}
    closes = {s: data[s]["close"].reindex(index).to_numpy() for s in symbols}
    sigs = {s: signals[s].reindex(index).to_numpy(dtype=float) for s in symbols}

    pf = Portfolio(cash=cash, commission=commission, slippage_bps=slippage_bps, fractional=fractional)
    weights = {s: 1 / len(symbols) for s in symbols}
    pending: dict[str, int] = {}
    last_close: dict[str, float] = {}
    equity = np.empty(len(index))
    invested_bars = 0

    for i, ts in enumerate(index):
        # 1) execute yesterday's decisions at today's open
        tradable = {s: d for s, d in pending.items() if not np.isnan(opens[s][i])}
        if tradable:
            open_prices = {s: opens[s][i] for s in tradable}
            pf.rebalance(tradable, {**last_close, **open_prices}, ts, allocation, weights)
            for s in tradable:
                pending.pop(s)

        # 2) mark to market at the close and decide for tomorrow
        for s in symbols:
            c = closes[s][i]
            if not np.isnan(c):
                last_close[s] = c
                pending[s] = int(sigs[s][i]) if not np.isnan(sigs[s][i]) else 0
        equity[i] = pf.equity(last_close) if last_close else cash
        invested_bars += bool(pf.positions)

    eq = pd.Series(equity, index=index, name="equity")
    result = BacktestResult(strategy, symbols, eq, pf.trades, signals)
    result.metrics = compute_metrics(eq, pf.trades, exposure=invested_bars / len(index))
    first = {s: df["close"].iloc[0] for s, df in data.items()}
    last = {s: df["close"].iloc[-1] for s, df in data.items()}
    result.metrics["benchmark_return"] = float(np.mean([last[s] / first[s] - 1 for s in symbols]))
    return result
