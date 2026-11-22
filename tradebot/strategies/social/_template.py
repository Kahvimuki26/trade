"""TEMPLATE: copy this file to e.g. ``my_tiktok_idea.py`` (no leading underscore).

Steps:
  1. Pick a unique ``name``. That is what you use in configs and the CLI.
  2. Note where you got it in ``source`` so you can find the video again.
  3. Put every tunable number in ``default_params``.
  4. Write ``generate_signals``: return 1 (long), 0 (flat) or -1 (short) per bar.
     ONLY use the current and past bars. Never use ``shift(-1)``, ``.iloc[-1]`` inside
     a vectorised rule, or other future data. ``tests/test_strategies.py``
     checks every registered strategy for look-ahead automatically.
  5. Try it:  tradebot backtest my_tiktok_idea SPY --start 2015-01-01
              tradebot compare SPY

Two handy patterns:
  * "long while condition holds":         (condition).astype(int)
  * "enter when X, exit when Y":          latch(X, Y)
"""

from __future__ import annotations

import pandas as pd

from ... import indicators as ind
from ..base import Strategy, latch, register


# @register    <- uncomment in your copy
class MyStrategy(Strategy):
    name = "my_strategy"
    description = "What it does in one line."
    source = "tiktok @creator, video link"
    display_name = "TikTok: My Strategy"          # what the dashboard shows
    explainer = {                                 # the dashboard's info button
        "summary": "One sentence a beginner understands.",
        "how": "The rules in plain words.",
        "good": "Markets where it should do well.",
        "bad": "Markets where it should struggle.",
    }
    param_help = {"rsi_period": "What changing this does."}
    default_params = {"rsi_period": 14, "buy_below": 30, "sell_above": 60, "trend": 200}

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        close = df["close"]
        r = ind.rsi(close, self.rsi_period)
        uptrend = close > ind.sma(close, self.trend)
        entries = (r < self.buy_below) & uptrend
        exits = r > self.sell_above
        return latch(entries, exits)
