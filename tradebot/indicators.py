"""Causal technical indicators (each value only uses current and past bars)."""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    out = 100 - 100 / (1 + avg_gain / avg_loss)
    return out.mask((avg_loss == 0) & avg_gain.notna(), 100.0)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """Return (macd_line, signal_line, histogram)."""
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    """Return (lower, middle, upper)."""
    mid = sma(close, n)
    std = close.rolling(n, min_periods=n).std(ddof=0)
    return mid - k * std, mid, mid + k * std


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def highest(s: pd.Series, n: int) -> pd.Series:
    """Highest value of the *previous* ``n`` bars (excludes the current bar)."""
    return s.rolling(n, min_periods=n).max().shift(1)


def lowest(s: pd.Series, n: int) -> pd.Series:
    """Lowest value of the *previous* ``n`` bars (excludes the current bar)."""
    return s.rolling(n, min_periods=n).min().shift(1)


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP for intraday bars (resets every calendar day)."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv = typical * df["volume"]
    day = ny_index(df).normalize()
    return pv.groupby(day).cumsum() / df["volume"].groupby(day).cumsum()


def ny_index(df: pd.DataFrame) -> pd.DatetimeIndex:
    """Bar timestamps in New York time (naive timestamps are assumed to already be NY)."""
    return df.index.tz_convert("America/New_York") if df.index.tz is not None else df.index


def minutes_of_day(df: pd.DataFrame):
    """Minutes since midnight (New York) for every bar, as a numpy array."""
    idx = ny_index(df)
    return (idx.hour * 60 + idx.minute).to_numpy()


def hhmm(s: str) -> int:
    h, m = map(int, s.split(":"))
    return h * 60 + m


def bar_minutes(df: pd.DataFrame) -> int:
    """Typical bar length in minutes (e.g. 1 or 5)."""
    if len(df) < 2:
        return 1
    return max(1, int(pd.Series(df.index).diff().median().total_seconds() // 60))


def latch(entries: pd.Series, exits: pd.Series, value: int = 1) -> pd.Series:
    """Hold ``value`` from an entry bar until an exit bar, else 0 (exit wins ties)."""
    state = pd.Series(np.nan, index=entries.index)
    state[entries.fillna(False).astype(bool)] = value
    state[exits.fillna(False).astype(bool)] = 0
    return state.ffill().fillna(0).astype(int)
