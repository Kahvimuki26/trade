import numpy as np
import pandas as pd
import pytest


def make_bars(n=800, seed=0, start="2020-01-01", freq="B", drift=0.0004, vol=0.015, price=100.0):
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, n)
    close = price * np.exp(np.cumsum(rets))
    open_ = close * np.exp(rng.normal(0, vol / 3, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, vol / 2, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, vol / 2, n)))
    idx = pd.date_range(start, periods=n, freq=freq)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": rng.integers(1e5, 1e6, n).astype(float)}, index=idx)


@pytest.fixture
def bars():
    return make_bars()


def make_intraday_bars(days=120, seed=0, start="2025-01-02", minutes=5, vol=0.0015, price=500.0):
    """Regular-session bars (9:30-16:00 New York) for ``days`` business days, tz-aware."""
    rng = np.random.default_rng(seed)
    per_day = int(390 / minutes)
    stamps = []
    for d in pd.bdate_range(start, periods=days):
        open_ = pd.Timestamp(d.date()).tz_localize("America/New_York") + pd.Timedelta(hours=9, minutes=30)
        stamps.extend(open_ + pd.Timedelta(minutes=minutes * k) for k in range(per_day))
    n = len(stamps)
    rets = rng.normal(0.00002, vol, n)
    rets[::per_day] += rng.normal(0, vol * 4, days)  # overnight gaps
    close = price * np.exp(np.cumsum(rets))
    open_ = np.r_[price, close[:-1]] * np.exp(rng.normal(0, vol / 4, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, vol / 2, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, vol / 2, n)))
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": rng.integers(1e4, 1e5, n).astype(float)},
                        index=pd.DatetimeIndex(stamps))


def bars_for(strategy_cls, seed=0, size="normal"):
    """Test data in the bar size a strategy needs."""
    if strategy_cls.interval and strategy_cls.interval.endswith("m"):
        days = {"small": 40, "normal": 80, "large": 150}[size]
        return make_intraday_bars(days=days, seed=seed, minutes=int(strategy_cls.interval[:-1]))
    n = {"small": 300, "normal": 800, "large": 1500}[size]
    return make_bars(n=n, seed=seed)
