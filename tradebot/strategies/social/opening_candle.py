"""TikTok: "the only strategy I'd learn. One candle, one rule."

Transcript summary:
  * At 9:30 New York, look only at the FIRST 5-minute candle of the day.
  * It closes above the fast moving average -> go long. Below -> go short.
  * Stop far away so normal noise doesn't kick you out. It trails as the trade runs.
  * If the trend holds you stay in, sometimes for days.
  * Risk 1% of the account per trade, never more.
  * Claimed: tested since 2019, 1,792 trades, +505.8%, 56% winners.

The video does NOT say which moving average, how far the stop is, or what
happens when tomorrow's first candle disagrees with an open trade. Those are
parameters here, with these defaults:
  * fast MA      = EMA(9) on the 5-minute chart
  * stop         = 1.5 x daily ATR(14) from the entry candle's close, trailing
                   behind the close (ATR from *completed* days only)
  * disagreement = reverse the position (the trend didn't "hold")
  * size         = 1% risk: weight = risk / stop distance, capped by max_leverage
"""

from __future__ import annotations

from datetime import time

import numpy as np
import pandas as pd

from ... import indicators as ind
from ..base import Strategy, register

NY = "America/New_York"


def daily_atr_prev(df: pd.DataFrame, n: int) -> pd.Series:
    """Daily ATR(n) from intraday bars, using only days *before* each bar's day."""
    idx = df.index.tz_convert(NY) if df.index.tz is not None else df.index
    day = pd.Series(idx.normalize(), index=df.index)
    daily = df.groupby(day.values).agg(high=("high", "max"), low=("low", "min"), close=("close", "last"))
    a = ind.atr(daily, n).shift(1)  # known at the open of each day
    return pd.Series(a.reindex(day.values).to_numpy(), index=df.index)


@register
class OpeningCandle(Strategy):
    name = "opening_candle"
    description = "First 5-min candle vs fast EMA: long above, short below. Wide trailing stop, 1% risk."
    source = "TikTok ('If I had to start from zero again, I'd only learn this one strategy')"
    interval = "5m"
    default_params = {
        "ma_period": 9,            # fast EMA length, in 5-minute bars
        "ma_type": "ema",          # "ema" or "sma"
        "atr_days": 14,            # daily ATR lookback used for the stop distance
        "stop_atr": 1.5,           # stop distance = stop_atr x daily ATR
        "trail": True,             # stop follows the trade
        "risk": 0.01,              # fraction of equity risked per trade (1%)
        "reverse_on_opposite": True,  # next day's first candle disagrees -> flip
        "allow_short": True,
    }
    recommended = {"symbols": ["SPY", "QQQ"], "max_leverage": 2.0}
    notes = (
        "Needs 5-minute bars. yfinance only has ~60 days of 5m history, so use "
        "--source alpaca for a real backtest (Alpaca has 5m bars back to 2016; set "
        "ALPACA_DATA_FEED=sip for full market volume).\n"
        "The video doesn't name the moving average or the stop distance; defaults are "
        "EMA(9) and 1.5x daily ATR. Tune them with -p ma_period=20 -p stop_atr=2.\n"
        "The stop is managed by the bot, not a resting order: it's checked on every "
        "completed 5m bar (backtest: exit fills at the next bar's open).\n"
        "Sizing: 1% risk / stop distance. On SPY that's often 50-100% of the account, "
        "so max_leverage 2 lets it take full size in quiet markets.\n"
        "The +505.8% / 1,792 trades claim is the creator's own and unverified; "
        "they don't say which market (probably index futures, not SPY)."
    )

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        if len(df) > 1 and (df.index[1] - df.index[0]) >= pd.Timedelta(hours=20):
            raise ValueError("opening_candle needs intraday 5-minute bars (use --interval 5m)")
        close, high, low = df["close"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy()
        ma_fn = ind.ema if self.ma_type == "ema" else ind.sma
        ma = ma_fn(df["close"], self.ma_period).to_numpy()
        datr = daily_atr_prev(df, self.atr_days).to_numpy()

        idx = df.index.tz_convert(NY) if df.index.tz is not None else df.index
        is_first = np.asarray(idx.time == time(9, 30))

        n = len(df)
        signal = np.zeros(n, dtype=int)
        weight = np.full(n, np.nan)
        pos, stop, w = 0, np.nan, np.nan

        for i in range(n):
            dist = self.stop_atr * datr[i]
            # 1) manage an open trade: stop hit? else trail it
            if pos == 1:
                if low[i] <= stop:
                    pos = 0
                elif self.trail and np.isfinite(dist):
                    stop = max(stop, close[i] - dist)
            elif pos == -1:
                if high[i] >= stop:
                    pos = 0
                elif self.trail and np.isfinite(dist):
                    stop = min(stop, close[i] + dist)

            # 2) the one candle that matters
            if is_first[i] and np.isfinite(ma[i]) and np.isfinite(dist) and dist > 0:
                want = 1 if close[i] > ma[i] else -1 if close[i] < ma[i] else 0
                if want == -1 and not self.allow_short:
                    want = 0
                if want and (pos == 0 or (pos != want and self.reverse_on_opposite)):
                    pos = want
                    stop = close[i] - dist if want == 1 else close[i] + dist
                    w = self.risk * close[i] / dist

            signal[i] = pos
            weight[i] = w if pos else np.nan

        return pd.DataFrame({"signal": signal, "weight": weight}, index=df.index)
