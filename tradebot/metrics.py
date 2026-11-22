"""Performance statistics for an equity curve."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .portfolio import Trade


def periods_per_year(index: pd.DatetimeIndex) -> float:
    if len(index) < 2:
        return 252.0
    step = pd.Series(index).diff().median().total_seconds()
    if step >= 6 * 86400:
        return 52.0
    if step >= 20 * 3600:
        return 252.0
    return 252.0 * 6.5 * 3600 / step  # intraday: regular session bars


def compute_metrics(equity: pd.Series, trades: list[Trade], exposure: float | None = None) -> dict:
    equity = equity.dropna()
    start, end = float(equity.iloc[0]), float(equity.iloc[-1])
    rets = equity.pct_change().dropna()
    ppy = periods_per_year(equity.index)

    days = max((equity.index[-1] - equity.index[0]).days, 1)
    years = days / 365.25
    total = end / start - 1
    cagr = (end / start) ** (1 / years) - 1 if end > 0 and years > 0 else float("nan")
    vol = rets.std() * math.sqrt(ppy) if len(rets) > 1 else float("nan")
    sharpe = rets.mean() / rets.std() * math.sqrt(ppy) if len(rets) > 1 and rets.std() > 0 else float("nan")
    downside = rets[rets < 0].std()
    sortino = rets.mean() / downside * math.sqrt(ppy) if downside and downside > 0 else float("nan")
    drawdown = equity / equity.cummax() - 1
    max_dd = float(drawdown.min())

    closed = [t.realized_pnl for t in trades if t.realized_pnl is not None]
    wins = [p for p in closed if p > 0]
    losses = [p for p in closed if p <= 0]
    profit_factor = sum(wins) / -sum(losses) if losses and sum(losses) < 0 else float("inf") if wins else float("nan")

    return {
        "start_equity": start,
        "end_equity": end,
        "total_return": total,
        "cagr": cagr,
        "volatility": vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "calmar": cagr / -max_dd if max_dd < 0 else float("nan"),
        "orders": len(trades),
        "round_trips": len(closed),
        "win_rate": len(wins) / len(closed) if closed else float("nan"),
        "avg_win": float(np.mean(wins)) if wins else float("nan"),
        "avg_loss": float(np.mean(losses)) if losses else float("nan"),
        "profit_factor": profit_factor,
        "exposure": exposure if exposure is not None else float("nan"),
    }


PERCENT_KEYS = {"total_return", "cagr", "volatility", "max_drawdown", "win_rate", "exposure", "benchmark_return"}


def fmt(key: str, value) -> str:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return "-" if math.isnan(value) else "inf"
    if key in PERCENT_KEYS:
        return f"{value * 100:.2f}%"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)
