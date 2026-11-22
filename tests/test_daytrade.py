from datetime import time

import numpy as np
import pytest

from tradebot.backtest import run_backtest
from tradebot.strategies import get_strategy

from .conftest import make_bars, make_intraday_bars


def ny(idx):
    return idx.tz_convert("America/New_York")


@pytest.mark.parametrize("name,minutes", [("dip_scalper", 1), ("orb_breakout", 5)])
def test_flat_before_close_every_day(name, minutes):
    bars = make_intraday_bars(days=30, seed=4, minutes=minutes, vol=0.002 * minutes ** 0.5)
    res = run_backtest(get_strategy(name), {"X": bars})
    assert res.metrics["orders"] > 0
    # every fill happens during the same session and nothing is held at the last bar of a day
    sig = res.signals["X"]
    last_bar_of_day = sig.groupby(ny(sig.index).date).tail(1)
    assert (last_bar_of_day == 0).all()
    for t in res.trades:
        assert time(9, 30) <= ny(__import__("pandas").DatetimeIndex([t.ts]))[0].time() < time(16, 0)


def test_scalper_trades_many_times_and_respects_limits():
    bars = make_intraday_bars(days=20, seed=9, minutes=1, vol=0.0012)
    strat = get_strategy("dip_scalper", max_trades_per_day=5)
    t = strat.targets(bars)
    entries = t[(t["signal"] == 1) & (t["signal"].shift(fill_value=0) == 0)]
    per_day = entries.groupby(ny(entries.index).date).size()
    assert per_day.max() <= 5 and len(entries) > 10
    times = ny(entries.index).time
    assert min(times) >= time(9, 45) and max(times) < time(15, 30)
    assert (t["weight"].dropna() == 1.0).all()


def test_scalper_stop_and_target():
    bars = make_intraday_bars(days=20, seed=1, minutes=1, vol=0.0012)
    res = run_backtest(get_strategy("dip_scalper", max_hold_min=10_000), {"X": bars}, slippage_bps=0)
    pnl = [t.realized_pnl / (abs(t.qty) * t.price) for t in res.trades if t.realized_pnl is not None]
    # exits fill at the next bar's open, so allow a little overshoot past +0.4% / -0.3%
    assert max(pnl) < 0.012 and min(pnl) > -0.012
    assert any(p > 0.003 for p in pnl) and any(p < -0.002 for p in pnl)


def test_orb_one_trade_per_day_and_both_directions():
    bars = make_intraday_bars(days=60, seed=3, minutes=5, vol=0.003)
    t = get_strategy("orb_breakout").targets(bars)
    sig = t["signal"]
    entries = sig[(sig != 0) & (sig.shift(fill_value=0) == 0)]
    assert entries.groupby(ny(entries.index).date).size().max() == 1
    assert {1, -1} <= set(entries.unique())
    assert (ny(entries.index).time >= time(9, 45)).all()
    long_only = get_strategy("orb_breakout", allow_short=False).signals(bars)
    assert long_only.min() == 0


def test_intraday_only():
    for name in ("dip_scalper", "orb_breakout"):
        with pytest.raises(ValueError, match="bars"):
            get_strategy(name).targets(make_bars(n=50))


def test_recommended_slippage_used_in_config(tmp_path):
    from tradebot.config import load_config

    p = tmp_path / "c.yaml"
    p.write_text("instances:\n  - {name: s, strategy: dip_scalper}\n")
    inst = load_config(p).instances[0]
    assert (inst.interval, inst.symbols, inst.slippage_bps) == ("1m", ["TQQQ"], 3)
