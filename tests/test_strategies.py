import pandas as pd
import pytest

from tradebot.strategies import get_strategy, latch, list_strategies, strategy_class

from .conftest import bars_for

ALL = [s.name for s in list_strategies()]


def test_registry_has_classics():
    for name in ["buy_and_hold", "sma_cross", "rsi_reversion", "macd_cross",
                 "bollinger_reversion", "donchian_breakout", "ema_cross_trend", "opening_candle"]:
        assert name in ALL
    assert "my_strategy" not in ALL  # template is not registered


@pytest.mark.parametrize("name", ALL)
def test_signals_are_valid(name):
    bars = bars_for(strategy_class(name))
    t = get_strategy(name).targets(bars)
    assert t.index.equals(bars.index)
    assert set(t["signal"].unique()) <= {-1, 0, 1}
    assert (t["weight"].dropna() >= 0).all()


@pytest.mark.parametrize("name", ALL)
def test_no_lookahead(name):
    """Signals up to bar t must not change when future bars are appended."""
    full = bars_for(strategy_class(name), seed=3)
    strat = get_strategy(name)
    full_t = strat.targets(full)
    n = len(full)
    for cut in (n // 3, n // 2 + 7, n - 1):
        partial = strat.targets(full.iloc[:cut])
        pd.testing.assert_frame_equal(partial, full_t.iloc[:cut],
                                      obj=f"{name} signals/weights truncated at {cut}")


@pytest.mark.parametrize("name", ALL)
def test_some_trading_happens(name):
    sig = get_strategy(name).signals(bars_for(strategy_class(name), seed=7, size="large"))
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


@pytest.mark.parametrize("name", ALL)
def test_beginner_info_present(name):
    cls = strategy_class(name)
    assert cls.display_name, f"{name}: add display_name"
    assert cls.explainer.get("summary") and cls.explainer.get("how"), f"{name}: add explainer summary/how"
    missing = set(cls.default_params) - set(cls.param_help)
    assert not missing, f"{name}: add param_help for {sorted(missing)}"
