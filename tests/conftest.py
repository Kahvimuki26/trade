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
