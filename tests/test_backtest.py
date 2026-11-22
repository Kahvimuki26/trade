import pandas as pd
import pytest

from tradebot.backtest import run_backtest
from tradebot.portfolio import Portfolio
from tradebot.strategies import Strategy, get_strategy

from .conftest import make_bars


def test_buy_and_hold_matches_price():
    df = make_bars(n=300)
    res = run_backtest(get_strategy("buy_and_hold"), {"X": df}, cash=10_000, slippage_bps=0)
    # buys at the open of bar 1, holds to the end
    expected = 10_000 * df["close"].iloc[-1] / df["open"].iloc[1]
    assert res.equity.iloc[-1] == pytest.approx(expected)
    assert res.equity.iloc[0] == 10_000
    assert len(res.trades) == 1


def test_signal_executes_next_open():
    df = make_bars(n=10)

    class OnOff(Strategy):
        name = "_onoff"

        def generate_signals(self, d):
            s = pd.Series(0, index=d.index)
            s.iloc[3:6] = 1
            return s

    res = run_backtest(OnOff(), {"X": df}, slippage_bps=0)
    buy, sell = res.trades
    assert pd.Timestamp(buy.ts) == df.index[4] and buy.price == df["open"].iloc[4]
    assert pd.Timestamp(sell.ts) == df.index[7] and sell.price == df["open"].iloc[7]
    assert sell.realized_pnl == pytest.approx((df["open"].iloc[7] - df["open"].iloc[4]) * buy.qty)


def test_multi_symbol_and_costs():
    data = {"A": make_bars(seed=1), "B": make_bars(seed=2)}
    free = run_backtest(get_strategy("sma_cross"), data, slippage_bps=0)
    costly = run_backtest(get_strategy("sma_cross"), data, slippage_bps=10, commission=1)
    assert costly.metrics["end_equity"] < free.metrics["end_equity"]
    assert {t.symbol for t in free.trades} == {"A", "B"}


def test_portfolio_rebalance_idempotent_and_short():
    pf = Portfolio(cash=1000, slippage_bps=0)
    pf.rebalance({"A": 1}, {"A": 10.0}, "t0")
    assert pf.qty("A") == pytest.approx(100)
    assert pf.rebalance({"A": 1}, {"A": 12.0}, "t1") == []
    pf.rebalance({"A": -1}, {"A": 12.0}, "t2")  # flip to short
    assert pf.qty("A") < 0
    assert pf.trades[1].realized_pnl == pytest.approx(200)
    pf.rebalance({"A": 0}, {"A": 11.0}, "t3")
    assert pf.positions == {}
    assert pf.cash == pytest.approx(1200 + 1200 / 12 * 1)  # short gained 1/share


def test_whole_shares():
    pf = Portfolio(cash=1000, slippage_bps=0, fractional=False)
    pf.rebalance({"A": 1}, {"A": 300.0}, "t")
    assert pf.qty("A") == 3


def test_portfolio_roundtrip_serialization():
    pf = Portfolio(cash=1000)
    pf.rebalance({"A": 1}, {"A": 10.0}, "t")
    again = Portfolio.from_dict(pf.to_dict())
    assert again == pf
