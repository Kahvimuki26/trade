"""Five classic strategies plus a buy-and-hold benchmark."""

from __future__ import annotations

import pandas as pd

from ... import indicators as ind
from ..base import Strategy, latch, register


@register
class BuyAndHold(Strategy):
    name = "buy_and_hold"
    description = "Always long. The benchmark every other strategy has to beat."
    source = "classic"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        return pd.Series(1, index=df.index)


@register
class SmaCross(Strategy):
    name = "sma_cross"
    description = "Long while the fast SMA is above the slow SMA (trend following)."
    source = "classic"
    default_params = {"fast": 20, "slow": 50}

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        fast = ind.sma(df["close"], self.fast)
        slow = ind.sma(df["close"], self.slow)
        return (fast > slow).astype(int)


@register
class RsiReversion(Strategy):
    name = "rsi_reversion"
    description = "Buy when RSI drops below 'oversold', sell when it rises above 'overbought'."
    source = "classic"
    default_params = {"period": 14, "oversold": 30, "overbought": 70}

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        r = ind.rsi(df["close"], self.period)
        return latch(r < self.oversold, r > self.overbought)


@register
class MacdCross(Strategy):
    name = "macd_cross"
    description = "Long while the MACD line is above its signal line (momentum)."
    source = "classic"
    default_params = {"fast": 12, "slow": 26, "signal": 9}

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        line, sig, _ = ind.macd(df["close"], self.fast, self.slow, self.signal)
        return (line > sig).astype(int)


@register
class BollingerReversion(Strategy):
    name = "bollinger_reversion"
    description = "Buy a close below the lower Bollinger band, exit at the middle band."
    source = "classic"
    default_params = {"period": 20, "std": 2.0}

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        lower, mid, _ = ind.bollinger(df["close"], self.period, self.std)
        close = df["close"]
        return latch(close < lower, close > mid)


@register
class DonchianBreakout(Strategy):
    name = "donchian_breakout"
    description = "Turtle-style: buy a new N-bar high, exit on a new M-bar low."
    source = "classic"
    default_params = {"entry": 20, "exit": 10}

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        close = df["close"]
        entries = close > ind.highest(df["high"], self.entry)
        exits = close < ind.lowest(df["low"], self.exit)
        return latch(entries, exits)
