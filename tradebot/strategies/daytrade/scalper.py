"""Dip Scalper: buy sharp intraday dips big, sell the small bounce, cut losers fast."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ... import indicators as ind
from ..base import Strategy, register


@register
class DipScalper(Strategy):
    name = "dip_scalper"
    display_name = "Dip Scalper (day trading)"
    category = "day trading"
    description = "1-min chart: buy sharp dips below VWAP, take a quick small profit, cut losses fast. Many trades a day."
    source = "classic day-trading setup (scalping)"
    interval = "1m"
    default_params = {
        "dip_pct": 0.004,       # how far below VWAP counts as a dip (0.4%)
        "rsi_period": 7,
        "rsi_below": 25,        # ...and short-term RSI must be this washed out
        "take_profit": 0.004,   # sell when up 0.4%
        "stop_loss": 0.003,     # sell when down 0.3%
        "max_hold_min": 30,     # give up on a trade after this many minutes
        "cooldown_min": 3,      # wait after a trade before the next one
        "max_trades_per_day": 20,
        "size": 1.0,            # fraction of the account per trade (1.0 = all in)
        "start": "09:45",       # skip the wild first 15 minutes
        "last_entry": "15:30",  # no new trades after this
        "flat_at": "15:55",     # sell everything before the close, never hold overnight
    }
    recommended = {"symbols": ["TQQQ"], "slippage_bps": 3}
    explainer = {
        "summary": "Buys sharp dips during the day with a big position, sells on a small bounce, and gets "
                   "out fast if it keeps falling. Can trade many times a day; always in cash by the close.",
        "how": "Watches 1-minute candles. VWAP is the day's average price, weighted by how much traded at "
               "each price; day traders use it as 'fair value' for the day. When the price drops 0.4% below VWAP "
               "and the short-term RSI says it fell unusually fast (below 25), it buys with the whole account. "
               "It sells at +0.4% (take profit), at -0.3% (stop loss), or after 30 minutes, whichever comes "
               "first. Then it waits a few minutes and looks for the next dip. Everything is sold at 15:55.",
        "sizing": "Each trade uses the whole account by default ('size' = 1.0). With several symbols, whichever "
                  "dips first gets the money; the others wait until cash is free (or set size to 0.5 each).",
        "good": "Choppy, range-bound days, when dips get bought back quickly. Liquid, volatile tickers "
                "(TQQQ, SPY, QQQ, NVDA, TSLA).",
        "bad": "Trend-down days: it keeps buying dips that keep dipping, taking stop after stop. Costs are "
               "the silent killer: with dozens of trades a day, a tiny cost per trade adds up fast.",
        "caveat": "Backtests flatter scalpers. Real fills, the bid/ask spread and delays matter a lot at this "
                  "speed, so slippage defaults to 3 bps per trade here. If it only wins with 0-1 bps, it doesn't "
                  "really win. Paper trade it for weeks before believing any number.",
    }
    param_help = {
        "dip_pct": "How far below VWAP counts as a dip (0.004 = 0.4%). Smaller = more trades.",
        "rsi_period": "Minutes the RSI looks back over.",
        "rsi_below": "RSI must be under this to buy. Lower = only the sharpest drops.",
        "take_profit": "Sell when up this much (0.004 = +0.4%).",
        "stop_loss": "Sell when down this much (0.003 = -0.3%). This is the 'cut losses fast' part.",
        "max_hold_min": "Sell after this many minutes even if neither target was hit.",
        "cooldown_min": "Minutes to wait after a trade before the next one.",
        "max_trades_per_day": "Stop trading for the day after this many trades.",
        "size": "Fraction of the account per trade. 1.0 = everything, 0.5 = half.",
        "start": "First time of day it may buy (New York time).",
        "last_entry": "Last time of day it may buy.",
        "flat_at": "Sell everything at this time. Never holds overnight.",
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        if len(df) > 1 and (df.index[1] - df.index[0]) >= pd.Timedelta(hours=1):
            raise ValueError("dip_scalper needs 1-minute bars (use --interval 1m)")
        close, high, low = df["close"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy()
        vw = ind.vwap(df).to_numpy()
        r = ind.rsi(df["close"], self.rsi_period).to_numpy()
        mins = ind.minutes_of_day(df)
        ends = mins + ind.bar_minutes(df)  # when each bar closes
        days = ind.ny_index(df).normalize().to_numpy()
        start, last_entry, flat_at = ind.hhmm(self.start), ind.hhmm(self.last_entry), ind.hhmm(self.flat_at)

        n = len(df)
        signal = np.zeros(n, dtype=int)
        weight = np.full(n, np.nan)
        pos, stop, target, held, cooldown, trades, day = 0, 0.0, 0.0, 0, 0, 0, None

        for i in range(n):
            if days[i] != day:
                day, trades, cooldown = days[i], 0, 0
                pos = 0  # never carry a position into a new day
            if pos:
                held += 1
                if low[i] <= stop or high[i] >= target or held >= self.max_hold_min or ends[i] >= flat_at:
                    pos, cooldown = 0, self.cooldown_min
            elif cooldown:
                cooldown -= 1
            elif (start <= mins[i] < last_entry and trades < self.max_trades_per_day
                  and np.isfinite(vw[i]) and np.isfinite(r[i])
                  and close[i] < vw[i] * (1 - self.dip_pct) and r[i] < self.rsi_below):
                pos, held, trades = 1, 0, trades + 1
                stop = close[i] * (1 - self.stop_loss)
                target = close[i] * (1 + self.take_profit)
            signal[i] = pos
            weight[i] = self.size if pos else np.nan

        return pd.DataFrame({"signal": signal, "weight": weight}, index=df.index)
