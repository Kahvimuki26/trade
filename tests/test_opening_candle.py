from datetime import time

import numpy as np
import pandas as pd
import pytest

from tradebot.backtest import run_backtest
from tradebot.strategies import get_strategy

from .conftest import make_bars, make_intraday_bars


@pytest.fixture
def bars():
    return make_intraday_bars(days=120, seed=11)


def test_entries_only_on_first_candle(bars):
    t = get_strategy("opening_candle").targets(bars)
    sig = t["signal"]
    changes = sig[sig.diff().fillna(sig) != 0]
    entries = changes[changes != 0]
    assert len(entries) > 10
    times = entries.index.tz_convert("America/New_York").time
    assert set(times) == {time(9, 30)}  # new positions only start on the 9:30 candle


def test_direction_matches_ema_rule(bars):
    from tradebot import indicators as ind

    strat = get_strategy("opening_candle", reverse_on_opposite=True)
    t = strat.targets(bars)
    ema = ind.ema(bars["close"], 9)
    first = bars.index.tz_convert("America/New_York").time == time(9, 30)
    for ts in bars.index[first][20:]:
        if np.isnan(t.loc[ts, "weight"]):
            continue
        expected = 1 if bars.loc[ts, "close"] > ema.loc[ts] else -1
        assert t.loc[ts, "signal"] == expected


def test_risk_sizing_is_one_percent(bars):
    t = get_strategy("opening_candle", risk=0.01, stop_atr=1.5).targets(bars)
    from tradebot.strategies.social.opening_candle import daily_atr_prev

    datr = daily_atr_prev(bars, 14)
    entry_bars = t.index[(t["signal"] != 0) & (t["signal"].diff().fillna(t["signal"]) != 0)]
    ts = entry_bars[3]
    stop_dist = 1.5 * datr.loc[ts]
    assert t.loc[ts, "weight"] == pytest.approx(0.01 * bars.loc[ts, "close"] / stop_dist)


def test_long_only_and_no_reverse(bars):
    t = get_strategy("opening_candle", allow_short=False).targets(bars)
    assert t["signal"].min() == 0
    stay = get_strategy("opening_candle", reverse_on_opposite=False).targets(bars)["signal"]
    flips = ((stay.shift() * stay) < 0).sum()
    assert flips == 0  # always goes through flat (stop) before switching sides


def test_stop_exits_position():
    bars = make_intraday_bars(days=60, seed=5)
    t = get_strategy("opening_candle", stop_atr=0.2, reverse_on_opposite=False).targets(bars)
    sig = t["signal"]
    exits = sig[(sig == 0) & (sig.shift() != 0)].index
    times = set(exits.tz_convert("America/New_York").time)
    assert len(exits) > 5 and times - {time(9, 30)}  # tight stop -> exits during the day


def test_backtest_risk_is_capped():
    bars = make_intraday_bars(days=120, seed=2)
    res = run_backtest(get_strategy("opening_candle"), {"SPY": bars}, max_leverage=2.0)
    assert res.metrics["orders"] > 10
    worst = min(t.realized_pnl for t in res.trades if t.realized_pnl is not None)
    # stop is checked on bar close and gaps exist, so allow some slippage past 1%
    assert worst > -0.03 * res.equity.max()


def test_rejects_daily_bars():
    with pytest.raises(ValueError, match="5-minute"):
        get_strategy("opening_candle").targets(make_bars(n=100))
