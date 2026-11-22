"""Example social-media strategy: "9/21 EMA cross, only trade above the 200 EMA".

One of the most common setups in trading TikToks. It's here as a worked
example of how to add your own.
"""

from __future__ import annotations

import pandas as pd

from ... import indicators as ind
from ..base import Strategy, register


@register
class EmaCrossTrend(Strategy):
    name = "ema_cross_trend"
    description = "Long while EMA9 > EMA21 and price is above the 200 EMA (trend filter)."
    source = "social media (common TikTok setup)"
    default_params = {"fast": 9, "slow": 21, "trend": 200}

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        close = df["close"]
        cross_up = ind.ema(close, self.fast) > ind.ema(close, self.slow)
        uptrend = close > ind.ema(close, self.trend)
        return (cross_up & uptrend).astype(int)
