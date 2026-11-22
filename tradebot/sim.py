"""Replay the autopilot on past prices, hour by hour, exactly as it would have run live.

Decisions are made at each candle's close and filled at the next candle's open, with
Binance-like fees. Stop-losses trigger inside the candle at the stop price (or the open,
if the price gapped below it).
"""

from __future__ import annotations

from collections import Counter

import numpy as np
import pandas as pd

from .autopilot import COST_BPS, RISK, Brain, analyze
from .data import MarketData
from .metrics import compute_metrics
from .portfolio import Portfolio

WARMUP_HOURS = 100


def simulate(market: MarketData, coins: list[str], days: int = 90, risk: str = "normal",
             cash: float = 100.0) -> dict:
    cfg = RISK[risk]
    now = pd.Timestamp.now(tz="UTC").floor("h")
    start = now - pd.Timedelta(days=days)
    fetch_from = start - pd.Timedelta(days=cfg["lookback_days"]) - pd.Timedelta(hours=WARMUP_HOURS)
    data = {c: market.history(c, fetch_from) for c in coins}
    data = {c: df for c, df in data.items() if len(df) > WARMUP_HOURS}
    if not data:
        raise ValueError("No price history for those coins")
    an = {c: analyze(df, cfg["lookback_days"]) for c, df in data.items()}

    index = pd.DatetimeIndex(sorted(set().union(*(df.index for df in data.values()))))
    index = index[index >= start]
    if len(index) < 2:
        raise ValueError("Not enough history for that period")
    cols = {c: an[c].columns for c in data}
    arr = {c: {k: an[c][k].reindex(index).to_numpy() for k in cols[c]} for c in data}
    o = {c: data[c]["open"].reindex(index).to_numpy() for c in data}
    lo = {c: data[c]["low"].reindex(index).to_numpy() for c in data}
    cl = {c: data[c]["close"].reindex(index).to_numpy() for c in data}

    pf = Portfolio(cash=cash, slippage_bps=COST_BPS, fractional=True)
    brain = Brain(risk)
    weights = {c: cfg["per_coin"] for c in data}
    pending: dict[str, int] | None = None
    last: dict[str, float] = {}
    equity = np.empty(len(index))
    used = Counter()
    stops = 0

    for i, ts in enumerate(index):
        opens = {c: o[c][i] for c in data if np.isfinite(o[c][i])}
        # 1) fill last hour's decisions at this candle's open
        if pending:
            tradable = {c: d for c, d in pending.items() if c in opens}
            pf.rebalance(tradable, {**last, **opens}, ts, weights=weights, max_leverage=1.0)
        # 2) stop-losses inside this candle
        for c in list(pf.positions):
            if c not in opens:
                continue
            stop = pf.avg_price[c] * (1 - cfg["stop"])
            if lo[c][i] <= stop:
                pf.order(c, -pf.positions[c], min(opens[c], stop), ts)
                brain.on_stop(c, ts)
                stops += 1
        # 3) decide at the close
        rows = {c: {k: arr[c][k][i] for k in cols[c]} for c in data if np.isfinite(cl[c][i])}
        for c in rows:
            last[c] = cl[c][i]
        targets, decisions = brain.step(rows, set(pf.positions), ts)
        for d in decisions:
            if d.coin in pf.positions or targets.get(d.coin):
                used[d.playbook] += 1
        pending = targets
        equity[i] = pf.equity(last)

    eq = pd.Series(equity, index=index)
    m = compute_metrics(eq, pf.trades)
    first = {c: data[c]["close"].reindex(index).bfill().iloc[0] for c in data}
    hold = sum(cash / len(data) * data[c]["close"].reindex(index).ffill() / first[c] for c in data)
    total_used = sum(used.values()) or 1
    step = max(1, len(index) // 600)
    return {
        "days": days, "risk": risk, "coins": list(data), "cash": cash,
        "end": float(eq.iloc[-1]), "return": m["total_return"], "hold_return": float(hold.iloc[-1] / cash - 1),
        "max_drawdown": m["max_drawdown"], "trades": m["round_trips"], "win_rate": m["win_rate"],
        "stops": stops,
        "playbook_share": {p: n / total_used for p, n in used.most_common()},
        "curve": [[t.isoformat(), float(v), float(h)] for t, v, h in
                  zip(index[::step], eq.to_numpy()[::step], hold.to_numpy()[::step])],
    }
