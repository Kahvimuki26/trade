"""Market data providers.

All providers return a DataFrame indexed by timestamp with lowercase columns
``open, high, low, close, volume``, sorted oldest to newest.

* ``yfinance``: free, no keys. Intraday history is limited (1m: ~7 days,
  5m/15m/30m: ~60 days, 1h: ~2 years).
* ``alpaca``: needs ALPACA_API_KEY / ALPACA_SECRET_KEY (free paper account).
  Uses the free IEX feed by default; set ALPACA_DATA_FEED=sip if you pay for it.
* ``csv``: reads ``<dir>/<SYMBOL>.csv``. Good for offline work and tests.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import pandas as pd

COLUMNS = ["open", "high", "low", "close", "volume"]
log = logging.getLogger("tradebot")


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns=str.lower)
    missing = set(COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"Data is missing columns: {sorted(missing)}")
    df = df[COLUMNS].astype(float).dropna(subset=["open", "high", "low", "close"])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.index.name = "timestamp"
    return df


def parse_interval(interval: str) -> tuple[int, str]:
    m = re.fullmatch(r"(\d+)\s*(m|min|h|d|wk)", interval.strip().lower())
    if not m:
        raise ValueError(f"Bad interval '{interval}'. Use e.g. 1m, 5m, 15m, 1h, 1d, 1wk")
    unit = {"min": "m"}.get(m.group(2), m.group(2))
    return int(m.group(1)), unit


class DataProvider:
    def get_bars(
        self, symbol: str, start: datetime | str, end: datetime | str | None = None, interval: str = "1d"
    ) -> pd.DataFrame:
        raise NotImplementedError


class YFinanceProvider(DataProvider):
    def get_bars(self, symbol, start, end=None, interval="1d"):
        import yfinance as yf

        n, unit = parse_interval(interval)
        if unit in ("m", "h"):  # Yahoo only keeps limited intraday history
            max_days = 7 if (unit == "m" and n == 1) else 59 if unit == "m" else 729
            earliest = utcnow() - timedelta(days=max_days)
            st = pd.Timestamp(start)
            if (st.tz_localize("UTC") if st.tzinfo is None else st) < earliest:
                log.warning("Yahoo only has ~%d days of %s bars; starting at %s. Use --source alpaca "
                            "for longer intraday history.", max_days, interval, earliest.date())
                start = earliest
        df = yf.Ticker(symbol).history(
            start=start, end=end, interval=f"{n}{unit}", auto_adjust=True, actions=False
        )
        if df.empty:
            raise ValueError(f"No data from Yahoo for {symbol} ({interval}, start={start})")
        if unit in ("d", "wk"):
            df.index = df.index.tz_localize(None).normalize()
        return _clean(df)


class AlpacaProvider(DataProvider):
    def __init__(self, api_key: str | None = None, secret_key: str | None = None, feed: str | None = None):
        try:
            from alpaca.data.historical import StockHistoricalDataClient
        except ImportError as e:  # pragma: no cover
            raise ImportError("Install alpaca-py:  pip install 'tradebot[alpaca]'") from e
        api_key = api_key or os.environ.get("ALPACA_API_KEY")
        secret_key = secret_key or os.environ.get("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY (see .env.example)")
        self.client = StockHistoricalDataClient(api_key, secret_key)
        self.feed = (feed or os.environ.get("ALPACA_DATA_FEED", "iex")).lower()

    def _end(self, end):
        # The free plan can read SIP (full market) data only if it's >15 minutes old.
        if end is not None:
            end = pd.Timestamp(end)
            end = end.tz_localize("UTC") if end.tzinfo is None else end
        if self.feed == "sip":
            latest = pd.Timestamp(utcnow() - timedelta(minutes=16))
            end = latest if end is None else min(end, latest)
        return end.to_pydatetime() if end is not None else None

    def get_bars(self, symbol, start, end=None, interval="1d"):
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        n, unit = parse_interval(interval)
        tf_unit = {"m": TimeFrameUnit.Minute, "h": TimeFrameUnit.Hour,
                   "d": TimeFrameUnit.Day, "wk": TimeFrameUnit.Week}[unit]
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(n, tf_unit),
            start=pd.Timestamp(start).to_pydatetime(),
            end=self._end(end),
            adjustment=Adjustment.ALL,
            feed=DataFeed(self.feed),
        )
        bars = self.client.get_stock_bars(req).df
        if bars.empty:
            raise ValueError(f"No data from Alpaca for {symbol} ({interval}, start={start})")
        df = bars.xs(symbol, level="symbol") if isinstance(bars.index, pd.MultiIndex) else bars
        df.index = pd.DatetimeIndex(df.index).tz_convert("America/New_York")
        if unit in ("d", "wk"):
            df.index = df.index.tz_localize(None).normalize()
        else:  # regular session only (Alpaca also returns pre/post-market bars)
            t = df.index.time
            df = df[(t >= dtime(9, 30)) & (t < dtime(16, 0))]
        return _clean(df)


class CsvProvider(DataProvider):
    """Reads ``<directory>/<SYMBOL>.csv`` with a date/timestamp column + OHLCV."""

    def __init__(self, directory: str | Path = "data"):
        self.directory = Path(directory)

    def get_bars(self, symbol, start=None, end=None, interval="1d"):
        path = self.directory / f"{symbol}.csv"
        df = pd.read_csv(path)
        ts_col = next(c for c in df.columns if c.lower() in ("date", "datetime", "timestamp", "time"))
        raw = df.pop(ts_col)
        try:
            idx = pd.DatetimeIndex(pd.to_datetime(raw))
        except ValueError:  # mixed UTC offsets, e.g. -05:00 / -04:00 across daylight saving
            idx = pd.DatetimeIndex(pd.to_datetime(raw, utc=True)).tz_convert("America/New_York")
        if idx.tz is not None:
            idx = idx.tz_convert("America/New_York")
        df.index = idx
        df = _clean(df)
        if start is not None:
            df = df[df.index >= _match_tz(start, df.index)]
        if end is not None:
            df = df[df.index <= _match_tz(end, df.index)]
        return df


def _match_tz(ts, index: pd.DatetimeIndex) -> pd.Timestamp:
    """Make ``ts`` comparable with ``index`` (both tz-aware or both naive)."""
    ts = pd.Timestamp(ts)
    if index.tz is None and ts.tzinfo is not None:
        return ts.tz_convert("America/New_York").tz_localize(None)
    if index.tz is not None and ts.tzinfo is None:
        return ts.tz_localize(index.tz)
    return ts


def get_provider(name: str, **kwargs) -> DataProvider:
    name = name.lower()
    if name in ("yfinance", "yahoo"):
        return YFinanceProvider()
    if name == "alpaca":
        return AlpacaProvider(**kwargs)
    if name == "csv":
        return CsvProvider(**kwargs)
    raise ValueError(f"Unknown data source '{name}'. Use yfinance, alpaca or csv.")


def default_lookback(interval: str) -> timedelta:
    """How much history to fetch for live signals (enough for a 200-bar indicator)."""
    n, unit = parse_interval(interval)
    if unit == "m":  # as much as Yahoo allows: daily-ATR style rules need weeks of intraday bars
        return timedelta(days=7 if n == 1 else 59)
    if unit == "h":
        return timedelta(days=120 * n)
    if unit == "wk":
        return timedelta(weeks=300 * n)
    return timedelta(days=450 * n)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
