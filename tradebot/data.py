"""Crypto market data from Binance's free public API (no account or keys needed).

Prices are in USDT, a dollar-pegged coin, so 1 USDT is about $1.
History is cached in data/cache/ so tests on past data don't re-download.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

import numpy as np
import pandas as pd

HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]
HOUR_MS = 3_600_000
CACHE_DIR = Path("data/cache")
COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "SUI", "LTC", "DOT", "TRX", "PEPE"]


def pair(coin: str) -> str:
    return coin.upper() + "USDT"


def _get(path: str, params: dict) -> object:
    q = urllib.parse.urlencode(params)
    err = None
    for host in HOSTS:
        try:
            req = urllib.request.Request(f"{host}{path}?{q}", headers={"User-Agent": "tradebot"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except Exception as e:  # try the next host
            err = e
    raise ConnectionError(f"Couldn't reach Binance ({err})")


def _frame(rows: list) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    a = np.array([r[:7] for r in rows], dtype=float)
    df = pd.DataFrame(a[:, 1:6], columns=["open", "high", "low", "close", "volume"],
                      index=pd.to_datetime(a[:, 0].astype("int64"), unit="ms", utc=True))
    df.index.name = "time"
    return df


class MarketData:
    """Interface: hourly candles (closed ones only) and live prices."""

    def candles(self, coin: str, bars: int = 800) -> pd.DataFrame:
        raise NotImplementedError

    def history(self, coin: str, start: pd.Timestamp) -> pd.DataFrame:
        raise NotImplementedError

    def prices(self, coins: list[str]) -> dict[str, float]:
        raise NotImplementedError


class Binance(MarketData):
    def __init__(self, cache_dir: Path = CACHE_DIR):
        self.cache_dir = cache_dir
        self._mem: dict[str, tuple[float, pd.DataFrame]] = {}
        self._lock = threading.Lock()

    def _klines(self, coin: str, start_ms: int | None = None, limit: int = 1000) -> pd.DataFrame:
        params = {"symbol": pair(coin), "interval": "1h", "limit": limit}
        if start_ms is not None:
            params["startTime"] = start_ms
        df = _frame(_get("/api/v3/klines", params))
        # drop the candle that's still forming
        now = pd.Timestamp.now(tz="UTC")
        return df[df.index + pd.Timedelta(hours=1) <= now]

    def candles(self, coin: str, bars: int = 800) -> pd.DataFrame:
        """The last ``bars`` closed hourly candles (cached for 5 minutes)."""
        with self._lock:
            hit = self._mem.get(coin)
            if hit and time.time() - hit[0] < 300 and len(hit[1]) >= bars:
                return hit[1].iloc[-bars:]
        start = int((pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=bars + 2)).timestamp() * 1000)
        df = self._fetch_range(coin, start)
        with self._lock:
            self._mem[coin] = (time.time(), df)
        return df.iloc[-bars:]

    def _fetch_range(self, coin: str, start_ms: int) -> pd.DataFrame:
        parts, cursor = [], start_ms
        while True:
            df = self._klines(coin, cursor)
            if df.empty:
                break
            parts.append(df)
            nxt = int(df.index[-1].timestamp() * 1000) + HOUR_MS
            if len(df) < 999 or nxt <= cursor:
                break
            cursor = nxt
        if not parts:
            raise ValueError(f"No price data for {coin}. Is {pair(coin)} traded on Binance?")
        out = pd.concat(parts)
        return out[~out.index.duplicated(keep="last")].sort_index()

    def history(self, coin: str, start: pd.Timestamp) -> pd.DataFrame:
        """Closed hourly candles from ``start`` until now, using the disk cache."""
        start = pd.Timestamp(start).tz_convert("UTC") if pd.Timestamp(start).tzinfo else pd.Timestamp(start, tz="UTC")
        path = self.cache_dir / f"{pair(coin)}_1h.csv"
        cached = pd.DataFrame()
        if path.exists():
            cached = pd.read_csv(path, index_col=0)
            cached.index = pd.to_datetime(cached.index, utc=True)
        if not cached.empty and cached.index[0] <= start:
            fetch_from = cached.index[-1] + pd.Timedelta(hours=1)
        else:
            fetch_from, cached = start, pd.DataFrame()
        if pd.Timestamp.now(tz="UTC") - fetch_from > pd.Timedelta(hours=1):
            new = self._fetch_range(coin, int(fetch_from.timestamp() * 1000))
            cached = pd.concat([cached, new]) if not cached.empty else new
            cached = cached[~cached.index.duplicated(keep="last")].sort_index()
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cached.to_csv(path)
        return cached[cached.index >= start]

    def prices(self, coins: list[str]) -> dict[str, float]:
        symbols = json.dumps([pair(c) for c in coins], separators=(",", ":"))
        rows = _get("/api/v3/ticker/price", {"symbols": symbols})
        by_pair = {r["symbol"]: float(r["price"]) for r in rows}
        return {c: by_pair[pair(c)] for c in coins if pair(c) in by_pair}


class Synthetic(MarketData):
    """Made-up prices with trending and choppy stretches. Used by tests and demos."""

    def __init__(self, hours: int = 24 * 200, seed: int = 0, end: pd.Timestamp | None = None):
        self.hours, self.seed = hours, seed
        self.end = (end or pd.Timestamp.now(tz="UTC")).floor("h")
        self._cache: dict[str, pd.DataFrame] = {}
        self.price_bump: dict[str, float] = {}

    def _series(self, coin: str) -> pd.DataFrame:
        if coin not in self._cache:
            rng = np.random.default_rng(zlib.crc32(f"{coin}:{self.seed}".encode()))
            n = self.hours
            drift = np.zeros(n)
            i = 0
            while i < n:  # alternate regimes: up-trend, chop, down-trend
                length = int(rng.integers(72, 400))
                drift[i:i + length] = rng.choice([0.0005, 0.0, -0.0005], p=[0.38, 0.32, 0.30])
                i += length
            vol = 0.007 + 0.006 * rng.random()
            rets = drift + rng.normal(0, vol, n)
            close = 100 * (1 + 10 * rng.random()) * np.exp(np.cumsum(rets))
            open_ = np.r_[close[0], close[:-1]]
            high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, vol / 2, n)))
            low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, vol / 2, n)))
            idx = pd.date_range(end=self.end - pd.Timedelta(hours=1), periods=n, freq="h", tz="UTC")
            self._cache[coin] = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                                              "volume": rng.integers(100, 1000, n).astype(float)}, index=idx)
        return self._cache[coin]

    def candles(self, coin: str, bars: int = 800) -> pd.DataFrame:
        return self._series(coin).iloc[-bars:]

    def history(self, coin: str, start: pd.Timestamp) -> pd.DataFrame:
        df = self._series(coin)
        return df[df.index >= start]

    def prices(self, coins: list[str]) -> dict[str, float]:
        return {c: float(self._series(c)["close"].iloc[-1]) * self.price_bump.get(c, 1.0) for c in coins}
