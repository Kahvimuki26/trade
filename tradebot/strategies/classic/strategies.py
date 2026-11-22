"""Five classic strategies plus a buy-and-hold benchmark."""

from __future__ import annotations

import pandas as pd

from ... import indicators as ind
from ..base import Strategy, latch, register


@register
class BuyAndHold(Strategy):
    name = "buy_and_hold"
    display_name = "Buy & Hold"
    description = "Always long. The benchmark every other strategy has to beat."
    source = "classic"
    explainer = {
        "summary": "Buy on day one and never sell. Not really a strategy: it's the yardstick.",
        "how": "Puts all the money into the stock(s) and holds through every rise and fall.",
        "good": "Markets that mostly go up over time, which US stock indexes historically have.",
        "bad": "Big crashes: you ride the whole drop. SPY fell about 34% in early 2020 and 25% in 2022.",
        "caveat": "If a strategy can't beat this after costs, you'd have been better off just holding.",
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        return pd.Series(1, index=df.index)


@register
class SmaCross(Strategy):
    name = "sma_cross"
    display_name = "Trend Follower (Moving Average Cross)"
    description = "Long while the fast SMA is above the slow SMA (trend following)."
    source = "classic"
    default_params = {"fast": 20, "slow": 50}
    explainer = {
        "summary": "Owns the stock while it's in an uptrend and sits in cash when it isn't.",
        "how": "A moving average is the average closing price over the last N days. It smooths out "
               "day-to-day noise. This strategy compares a short one (20 days, reacts quickly) with a long one "
               "(50 days, reacts slowly). Short above long means recent prices are higher than before, so it's "
               "an uptrend: buy. When the short one drops below the long one, it sells and waits in cash.",
        "good": "Long, steady trends, and big crashes: it usually gets out partway down instead of riding the "
                "whole drop.",
        "bad": "Choppy, sideways markets. It keeps buying high and selling low on false signals (called "
               "'whipsaws'). It's also always late: it buys after a move has started and sells after it has turned.",
    }
    param_help = {
        "fast": "Days in the short average. Smaller reacts faster but gives more false signals.",
        "slow": "Days in the long average. Bigger means fewer, slower trades.",
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        fast = ind.sma(df["close"], self.fast)
        slow = ind.sma(df["close"], self.slow)
        return (fast > slow).astype(int)


@register
class RsiReversion(Strategy):
    name = "rsi_reversion"
    display_name = "Buy the Dip (RSI)"
    description = "Buy when RSI drops below 'oversold', sell when it rises above 'overbought'."
    source = "classic"
    default_params = {"period": 14, "oversold": 30, "overbought": 70}
    explainer = {
        "summary": "Buys after a sharp drop and sells after a sharp rise, betting that big moves partly snap back.",
        "how": "RSI (Relative Strength Index) scores the last 14 days from 0 to 100 by comparing the size of "
               "up-days with down-days. Below 30 is 'oversold' (it fell hard and fast); above 70 is "
               "'overbought'. It buys when RSI dips under 30 and sells when it climbs back over 70.",
        "good": "Stocks or indexes that bounce around inside a rising trend, where dips tend to get bought.",
        "bad": "Real crashes and stocks in a long decline: 'oversold' can get a lot more oversold. There's no "
               "stop-loss, so it can sit on a losing position for a long time waiting for the bounce.",
    }
    param_help = {
        "period": "How many days RSI looks back over.",
        "oversold": "Buy when RSI falls below this. Lower means fewer but deeper dips.",
        "overbought": "Sell when RSI rises above this. Lower means quicker exits with smaller gains.",
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        r = ind.rsi(df["close"], self.period)
        return latch(r < self.oversold, r > self.overbought)


@register
class MacdCross(Strategy):
    name = "macd_cross"
    display_name = "Momentum (MACD)"
    description = "Long while the MACD line is above its signal line (momentum)."
    source = "classic"
    default_params = {"fast": 12, "slow": 26, "signal": 9}
    explainer = {
        "summary": "Rides momentum: in while the price is speeding up, out when it starts slowing down.",
        "how": "MACD is the 12-day average minus the 26-day average. These are exponential averages, which "
               "weight recent days more. That gap grows when the price accelerates upward. The 'signal line' "
               "is a 9-day average of the MACD itself. MACD above its signal line means momentum is "
               "improving, so it holds; below means it's fading, so it sells.",
        "good": "Strong trending periods.",
        "bad": "Sideways markets: lots of small losing trades that add up. It has the same weak spots as the "
               "trend follower, but it's faster and twitchier.",
    }
    param_help = {
        "fast": "Days in the fast average.",
        "slow": "Days in the slow average.",
        "signal": "Days in the signal-line average. Smaller reacts faster.",
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        line, sig, _ = ind.macd(df["close"], self.fast, self.slow, self.signal)
        return (line > sig).astype(int)


@register
class BollingerReversion(Strategy):
    name = "bollinger_reversion"
    display_name = "Snap Back (Bollinger Bands)"
    description = "Buy a close below the lower Bollinger band, exit at the middle band."
    source = "classic"
    default_params = {"period": 20, "std": 2.0}
    explainer = {
        "summary": "Buys when the price is unusually far below its average and sells once it's back to normal.",
        "how": "Bollinger Bands draw a 20-day average with a band above and below it, two standard deviations "
               "away. Think of it as the 'normal range', which widens when the market gets wild. A close "
               "below the lower band is unusual, so it buys there and sells once the price is back at the "
               "middle (the average).",
        "good": "Calm, range-bound markets. It usually has a high win rate: lots of small wins.",
        "bad": "When a drop keeps going, the 'unusual' price becomes the new normal and it holds a loss. "
               "Expect small wins with the occasional big loss.",
    }
    param_help = {
        "period": "Days in the average and the band calculation.",
        "std": "Band width in standard deviations. Bigger means rarer, more extreme buy signals.",
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        lower, mid, _ = ind.bollinger(df["close"], self.period, self.std)
        close = df["close"]
        return latch(close < lower, close > mid)


@register
class DonchianBreakout(Strategy):
    name = "donchian_breakout"
    display_name = "Breakout (Turtle)"
    description = "Turtle-style: buy a new N-bar high, exit on a new M-bar low."
    source = "classic"
    default_params = {"entry": 20, "exit": 10}
    explainer = {
        "summary": "Buys when the price breaks to a new high and sells when it breaks to a new low.",
        "how": "Made famous by the 1980s 'Turtle traders'. If today's close beats the highest price of the "
               "last 20 days, it buys, on the idea that breakouts often keep going. It sells when the close "
               "drops below the lowest price of the last 10 days.",
        "good": "Big, sustained trends: it catches most of the move.",
        "bad": "Many breakouts fail and reverse ('false breakouts'). Expect a low win rate (often 30-40%) "
               "and long losing streaks. It relies on a few big winners to pay for everything else.",
    }
    param_help = {
        "entry": "Buy on a new high over this many days. Bigger means fewer, stronger breakouts.",
        "exit": "Sell on a new low over this many days. Smaller means a tighter exit.",
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        close = df["close"]
        entries = close > ind.highest(df["high"], self.entry)
        exits = close < ind.lowest(df["low"], self.exit)
        return latch(entries, exits)
