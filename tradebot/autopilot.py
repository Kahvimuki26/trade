"""The autopilot: picks whichever playbook has been working lately, per coin, every hour.

How it decides (all on hourly candles, using only the past):
  1. Each playbook (ride the trend / catch breakouts / buy the dips) is replayed over the
     last N days for every coin, including trading costs.
  2. The one with the best risk-adjusted result wins that coin. If none of them made
     money lately, the coin gets "sit in cash".
  3. Coins whose winning playbook says "be in" are ranked by how well that playbook has
     been doing; the best few get the money (how many and how much depends on the risk level).
  4. Every position has a stop-loss. After a stop it leaves that coin alone for a few hours.

This is adaptive rules, not a crystal ball: it works when what worked last week keeps
working this week, and it gets caught out when the market suddenly flips.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import indicators as ind

FEE = 0.001        # Binance spot fee per trade (0.1%)
SPREAD = 0.0002    # extra cost of crossing the bid/ask
COST = FEE + SPREAD  # cost per buy or sell, as a fraction
COST_BPS = COST * 10_000
MIN_SCORE = 0.5    # a playbook needs at least this much 'edge' to be trusted
STICKY = 0.3       # keep the current playbook unless another beats it by this much
COOLDOWN_HOURS = 6

PLAYBOOKS = {
    "trend": {"label": "Ride the trend",
              "what": "Holds while the price is in an uptrend (fast average above slow average)."},
    "breakout": {"label": "Catch breakouts",
                 "what": "Buys when the price breaks above its 2-day high, sells when it breaks its 1-day low."},
    "dip": {"label": "Buy the dips",
            "what": "Buys sharp drops (unusually low price and RSI), sells once it bounces back to normal."},
    "cash": {"label": "Sit in cash",
             "what": "Nothing has been working on this coin lately, so it stays out."},
}
ACTIVE = ["trend", "breakout", "dip"]

RISK = {
    "safe": {"label": "Safe", "max_coins": 3, "per_coin": 0.20, "stop": 0.04, "lookback_days": 21,
             "blurb": "Up to 3 coins, 20% each (max 60% invested). Stop-loss 4%. Judges playbooks on 3 weeks."},
    "normal": {"label": "Normal", "max_coins": 2, "per_coin": 0.50, "stop": 0.06, "lookback_days": 14,
               "blurb": "Up to 2 coins, 50% each. Stop-loss 6%. Judges playbooks on 2 weeks."},
    "aggressive": {"label": "Aggressive", "max_coins": 1, "per_coin": 1.00, "stop": 0.08, "lookback_days": 7,
                   "blurb": "All-in on the single best coin. Stop-loss 8%. Judges playbooks on the last week, "
                            "so it switches fast."},
}


# ------------------------------------------------------------------ playbooks
def playbook_signals(df: pd.DataFrame) -> pd.DataFrame:
    """1 = be in, 0 = be out, per playbook, per hourly candle. Uses only past data."""
    close = df["close"]
    ema_fast, ema_slow = ind.ema(close, 20), ind.ema(close, 50)
    trend = ((ema_fast > ema_slow) & (close > ema_slow)).astype(int)

    breakout = ind.latch(close > ind.highest(df["high"], 48), close < ind.lowest(df["low"], 24))

    r = ind.rsi(close, 14)
    lower, mid, _ = ind.bollinger(close, 20, 2.0)
    dip = ind.latch((r < 30) & (close < lower), (close > mid) | (r > 60))

    return pd.DataFrame({"trend": trend, "breakout": breakout, "dip": dip}, index=df.index)


def analyze(df: pd.DataFrame, lookback_days: int) -> pd.DataFrame:
    """Per candle: each playbook's signal, its recent net return and its score, plus the market mood."""
    sigs = playbook_signals(df)
    ret = df["close"].pct_change().fillna(0)
    n = lookback_days * 24
    out = {}
    for p in ACTIVE:
        pos = sigs[p].shift(1).fillna(0)  # acting on the next candle
        net = pos * ret - COST * pos.diff().abs().fillna(0)
        total = net.rolling(n, min_periods=n).sum()
        vol = net.rolling(n, min_periods=n).std()
        out[f"sig_{p}"] = sigs[p]
        out[f"perf_{p}"] = total
        out[f"score_{p}"] = (total / (vol * math.sqrt(n))).where(vol > 0, 0.0)

    close = df["close"]
    change = close.diff(48).abs()
    path = close.diff().abs().rolling(48).sum()
    out["efficiency"] = (change / path).fillna(0)  # 1 = straight line, 0 = pure chop
    out["change_48h"] = close.pct_change(48)
    out["change_24h"] = close.pct_change(24)
    out["close"] = close
    return pd.DataFrame(out, index=df.index)


def mood(row) -> str:
    eff, ch = row.get("efficiency", 0) or 0, row.get("change_48h", 0) or 0
    if eff > 0.25 and ch > 0.02:
        return "Trending up"
    if eff > 0.25 and ch < -0.02:
        return "Trending down"
    return "Choppy / sideways"


# ------------------------------------------------------------------ the brain
@dataclass
class Decision:
    coin: str
    playbook: str
    signal: int
    score: float
    perf: dict
    mood: str
    action: str = "wait"   # buy / hold / sell / wait / skip
    reason: str = ""

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["label"] = PLAYBOOKS[self.playbook]["label"]
        return d


@dataclass
class Brain:
    risk: str = "normal"
    current: dict = field(default_factory=dict)  # coin -> playbook in use
    cooldown_until: dict = field(default_factory=dict)  # coin -> timestamp (iso)

    @property
    def cfg(self) -> dict:
        return RISK[self.risk]

    def pick(self, coin: str, row) -> Decision:
        perf = {p: _num(row.get(f"perf_{p}")) for p in ACTIVE}
        score = {p: _num(row.get(f"score_{p}")) for p in ACTIVE}
        best = max(ACTIVE, key=lambda p: score[p])
        prev = self.current.get(coin)
        if (prev in ACTIVE and prev != best and score[prev] >= MIN_SCORE and perf[prev] > 0
                and score[prev] >= score[best] - STICKY):
            best = prev  # don't flip-flop between two similar playbooks
        if score[best] < MIN_SCORE or perf[best] <= 0:
            best = "cash"
        self.current[coin] = best
        sig = int(row.get(f"sig_{best}", 0) or 0) if best != "cash" else 0
        return Decision(coin, best, sig, score.get(best, 0.0), perf, mood(row))

    def step(self, rows: dict, held: set[str], now: pd.Timestamp) -> tuple[dict[str, int], list[Decision]]:
        """Decide targets for all coins. Returns ({coin: 1/0}, decisions with reasons)."""
        cfg = self.cfg
        days = cfg["lookback_days"]
        decisions = [self.pick(c, row) for c, row in rows.items()]
        cooling = {c for c, t in self.cooldown_until.items() if pd.Timestamp(t) > now}
        wanted = [d for d in decisions if d.signal == 1 and d.coin not in cooling]
        wanted.sort(key=lambda d: (d.coin in held, d.score), reverse=True)  # keep what we hold first
        chosen = {d.coin for d in wanted[: cfg["max_coins"]]}

        targets = {}
        for d in decisions:
            pb = PLAYBOOKS[d.playbook]["label"]
            others = ", ".join(f"{PLAYBOOKS[p]['label'].lower()} {_pct(d.perf[p])}"
                               for p in ACTIVE if p != d.playbook and d.perf[p] is not None)
            track = (f"'{pb}' made {_pct(d.perf.get(d.playbook))} on {d.coin} over the last {days} days"
                     f" (vs {others})." if d.playbook != "cash" else
                     f"Nothing worked on {d.coin} over the last {days} days ({others}), so it stays out.")
            in_now = d.coin in chosen
            if d.coin in cooling:
                d.action, why = ("sell" if d.coin in held else "wait"), "Hit its stop-loss recently, so taking a short break."
            elif in_now:
                d.action = "hold" if d.coin in held else "buy"
                why = "The playbook says be in." if d.action == "hold" else "The playbook just said buy."
            elif d.signal == 1:
                d.action = "sell" if d.coin in held else "skip"
                why = f"Other coins look better right now (max {cfg['max_coins']} at once)."
            else:
                d.action = "sell" if d.coin in held else "wait"
                why = "The playbook says stay out for now." if d.playbook != "cash" else ""
            d.reason = f"{d.mood}. {track} {why}".strip()
            targets[d.coin] = 1 if in_now else 0
        return targets, decisions

    def on_stop(self, coin: str, now: pd.Timestamp) -> None:
        self.cooldown_until[coin] = (now + pd.Timedelta(hours=COOLDOWN_HOURS)).isoformat()

    def to_dict(self) -> dict:
        return {"risk": self.risk, "current": self.current, "cooldown_until": self.cooldown_until}

    @classmethod
    def from_dict(cls, d: dict) -> "Brain":
        return cls(d.get("risk", "normal"), d.get("current", {}), d.get("cooldown_until", {}))


def _num(x):
    try:
        x = float(x)
        return x if math.isfinite(x) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:+.1f}%"
