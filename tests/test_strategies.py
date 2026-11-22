import pandas as pd
import pytest

from tradebot.strategies import get_strategy, latch, list_strategies

from .conftest import make_bars

ALL = [s.name for s in list_strategies()]


def test_registry_has_classics():
    for name in ["buy_and_hold", "sma_cross", "rsi_reversion", "macd_cross",
                 "bollinger_reversion", "donchian_breakout", "ema_cross_trend"]:
        assert name in ALL
    assert "my_strategy" not in ALL  # template is not registered


@pytest.mark.parametrize("name", ALL)
def test_signals_are_valid(name, bars):
    sig = get_strategy(name).signals(bars)
    assert sig.index.equals(bars.index)
    assert set(sig.unique()) <= {-1, 0, 1}


@pytest.mark.parametrize("name", ALL)
def test_no_lookahead(name):
    """Signals up to bar t must not change when future bars are appended."""
    full = make_bars(n=700, seed=3)
    strat = get_strategy(name)
    full_sig = strat.signals(full)
    for cut in (250, 400, 699):
        partial = strat.signals(full.iloc[:cut])
        pd.testing.assert_series_equal(partial, full_sig.iloc[:cut], check_names=False,
                                       obj=f"{name} signals truncated at {cut}")


@pytest.mark.parametrize("name", ALL)
def test_some_trading_happens(name):
    sig = get_strategy(name).signals(make_bars(n=1500, seed=7))
    assert sig.abs().sum() > 0


def test_unknown_param_rejected():
    with pytest.raises(ValueError, match="Unknown parameter"):
        get_strategy("sma_cross", fats=10)


def test_param_override():
    s = get_strategy("sma_cross", fast=5)
    assert s.fast == 5 and s.slow == 50


def test_latch():
    e = pd.Series([0, 1, 0, 0, 0, 1, 1], dtype=bool)
    x = pd.Series([0, 0, 0, 1, 0, 0, 1], dtype=bool)
    assert latch(e, x).tolist() == [0, 1, 1, 0, 0, 1, 0]
