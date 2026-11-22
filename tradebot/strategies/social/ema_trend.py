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
    display_name = "TikTok: EMA Cross + Trend Filter"
    default_params = {"fast": 9, "slow": 21, "trend": 200}
    explainer = {
        "summary": "A popular TikTok setup: a short-term trend signal, only taken when the long-term trend is up.",
        "how": "Compares a 9-period and a 21-period exponential moving average. It's like the Trend Follower, "
               "but faster. The extra rule: only buy if the price is above its 200-period average, the classic "
               "'is this stock in a long-term uptrend?' test. It sells as soon as either condition fails.",
        "good": "Healthy bull markets.",
        "bad": "Choppy markets. The 200 filter keeps it out of most bear markets, but it also makes it "
               "late getting back in after a crash.",
    }
    param_help = {
        "fast": "Bars in the fast average.",
        "slow": "Bars in the slow average.",
        "trend": "Bars in the long-term trend filter (200 = about 10 months of daily bars).",
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        close = df["close"]
        cross_up = ind.ema(close, self.fast) > ind.ema(close, self.slow)
        uptrend = close > ind.ema(close, self.trend)
        return (cross_up & uptrend).astype(int)
