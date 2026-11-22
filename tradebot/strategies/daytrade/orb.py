"""Opening Range Breakout on leveraged ETFs: the 'go big or go home' day trade."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ... import indicators as ind
from ..base import Strategy, register


@register
class OpeningRangeBreakout(Strategy):
    name = "orb_breakout"
    display_name = "Opening Range Breakout (high risk)"
    category = "day trading"
    description = "Mark the first 15 minutes' high/low; go all-in on the breakout, stop at the other side, out by the close."
    source = "classic day-trading setup (ORB)"
    interval = "5m"
    default_params = {
        "range_minutes": 15,    # length of the opening range
        "reward_risk": 2.0,     # take profit at this many times the range height (0 = hold to close)
        "stop": "range",        # "range" = other side of the range, "mid" = middle (tighter)
        "allow_short": True,
        "max_trades_per_day": 1,
        "size": 1.0,            # fraction of the account per trade (1.0 = all in)
        "last_entry": "12:00",  # breakouts after lunch are weaker
        "flat_at": "15:55",
    }
    recommended = {"symbols": ["TQQQ", "SOXL"], "slippage_bps": 2}
    explainer = {
        "summary": "Waits for the first 15 minutes to set a range, then goes all-in on whichever way the price "
                   "breaks out. Big wins or quick losses; always in cash by the close.",
        "how": "From 9:30 to 9:45 it just records the highest and lowest price (the 'opening range'). After "
               "that, a 5-minute candle closing above the range high means buy; closing below the range low "
               "means short (bet on a fall). The stop-loss sits at the other side of the range. It takes profit "
               "when the move reaches 2x the range's height, or sells at 15:55 at the latest. One trade per day.",
        "sizing": "All in: the whole account goes into the trade (size = 1.0). With two symbols, whichever breaks "
                  "out first gets the money. By default it trades TQQQ and SOXL. "
                  "These are 3x leveraged funds: they move about three times as much as the Nasdaq 100 / "
                  "chip stocks each day, so a 2% market move is roughly a 6% move here. That's where the "
                  "'double it fast' potential comes from, and also the 'lose half fast' potential.",
        "good": "Trend days: news, earnings season, big macro days, when the first move keeps going all day.",
        "bad": "Fake-out days: it breaks out, reverses, and hits the stop. Those are common. Expect lots of "
               "small-to-medium losses paid for by a few big days.",
        "caveat": "3x leveraged funds lose value over time in choppy markets ('volatility decay'), so they're "
                  "for day trades, not holding. Turning $100 into $200 in days needs several big wins in a row. "
                  "It can happen, but the same setup can just as easily cut it to $50.",
    }
    param_help = {
        "range_minutes": "Length of the opening range in minutes (15 = 9:30-9:45).",
        "reward_risk": "Take profit at this many range heights from entry. 0 = no target, hold until close.",
        "stop": "range = stop at the opposite side; mid = stop at the middle (smaller losses, more stop-outs).",
        "allow_short": "Also bet on downside breakouts?",
        "max_trades_per_day": "Max trades per day per symbol.",
        "size": "Fraction of the account per trade. 1.0 = everything.",
        "last_entry": "No new trades after this time (New York).",
        "flat_at": "Sell everything at this time. Never holds overnight.",
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        if len(df) > 1 and (df.index[1] - df.index[0]) >= pd.Timedelta(hours=1):
            raise ValueError("orb_breakout needs intraday bars (use --interval 5m)")
        close, high, low = df["close"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy()
        mins = ind.minutes_of_day(df)
        ends = mins + ind.bar_minutes(df)  # when each bar closes
        days = ind.ny_index(df).normalize().to_numpy()
        range_end = ind.hhmm("09:30") + self.range_minutes
        last_entry, flat_at = ind.hhmm(self.last_entry), ind.hhmm(self.flat_at)

        n = len(df)
        signal = np.zeros(n, dtype=int)
        weight = np.full(n, np.nan)
        pos, stop, target, trades, day = 0, 0.0, 0.0, 0, None
        hi, lo = -np.inf, np.inf

        for i in range(n):
            if days[i] != day:
                day, trades, pos, hi, lo = days[i], 0, 0, -np.inf, np.inf
            if mins[i] < range_end:  # still building the opening range
                hi, lo = max(hi, high[i]), min(lo, low[i])
            elif pos == 1:
                if low[i] <= stop or high[i] >= target or ends[i] >= flat_at:
                    pos = 0
            elif pos == -1:
                if high[i] >= stop or low[i] <= target or ends[i] >= flat_at:
                    pos = 0
            elif trades < self.max_trades_per_day and mins[i] < last_entry and np.isfinite(hi) and hi > lo:
                height = hi - lo
                if close[i] > hi:
                    pos = 1
                    stop = lo if self.stop == "range" else (hi + lo) / 2
                    target = close[i] + self.reward_risk * height if self.reward_risk > 0 else np.inf
                elif close[i] < lo and self.allow_short:
                    pos = -1
                    stop = hi if self.stop == "range" else (hi + lo) / 2
                    target = close[i] - self.reward_risk * height if self.reward_risk > 0 else -np.inf
                if pos:
                    trades += 1
            signal[i] = pos
            weight[i] = self.size if pos else np.nan

        return pd.DataFrame({"signal": signal, "weight": weight}, index=df.index)
