from datetime import datetime, timezone

import pandas as pd

from tradebot.config import load_config
from tradebot.data import DataProvider
from tradebot.paper import PaperInstance, make_fetcher, market_is_open, run

from .conftest import make_bars


class FakeProvider(DataProvider):
    def __init__(self, n=400):
        self.n = n
        self.calls = 0

    def get_bars(self, symbol, start, end=None, interval="1d"):
        self.calls += 1
        return make_bars(n=self.n, seed=hash(symbol) % 100)


CONFIG = """
defaults:
  data_source: fake
  starting_cash: 5000
  state_dir: {state}
instances:
  - name: a
    strategy: buy_and_hold
    symbols: [SPY, QQQ]
  - name: b
    strategy: sma_cross
    params: {{fast: 5, slow: 20}}
    symbols: SPY
  - name: disabled-one
    strategy: sma_cross
    symbols: [SPY]
    enabled: false
"""


def write_cfg(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text(CONFIG.format(state=tmp_path / "state"))
    return load_config(p)


def test_config_defaults_merge(tmp_path):
    cfg = write_cfg(tmp_path)
    a, b, off = cfg.instances
    assert a.starting_cash == 5000 and b.symbols == ["SPY"] and b.params == {"fast": 5, "slow": 20}
    assert not off.enabled


def test_run_once_creates_independent_state(tmp_path):
    cfg = write_cfg(tmp_path)
    fake = FakeProvider()
    run(cfg, once=True, force=True, providers={"fake": fake})
    assert fake.calls == 2  # SPY fetched once and shared across instances
    a = PaperInstance(cfg.get("a"), cfg.state_dir)
    assert set(a.portfolio.positions) == {"SPY", "QQQ"}
    assert len(a.equity_history) == 1
    assert not (tmp_path / "state" / "disabled-one.json").exists()

    # second tick: buy-and-hold already in position -> no new trades, state persisted
    run(cfg, names=["a"], once=True, force=True, providers={"fake": fake})
    a = PaperInstance(cfg.get("a"), cfg.state_dir)
    assert len(a.portfolio.trades) == 2 and len(a.equity_history) == 2


def test_tick_matches_latest_signal(tmp_path):
    cfg = write_cfg(tmp_path)
    inst = PaperInstance(cfg.get("b"), cfg.state_dir)
    fake = FakeProvider()
    inst.tick(make_fetcher({"fake": fake}))
    expected = inst.strategy.signals(fake.get_bars("SPY", None)).iloc[-1]
    assert inst.portfolio.direction("SPY") == expected


def test_market_hours():
    ny_open = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)  # Mon 10:00 ET
    weekend = datetime(2026, 11, 22, 15, 0, tzinfo=timezone.utc)  # Sun
    night = datetime(2026, 11, 23, 23, 0, tzinfo=timezone.utc)
    assert market_is_open(ny_open) and not market_is_open(weekend) and not market_is_open(night)


def test_csv_provider_with_aware_start(tmp_path):
    from datetime import timedelta

    from tradebot.data import CsvProvider

    make_bars(n=50, start="2026-01-01").to_csv(tmp_path / "SPY.csv", index_label="date")
    df = CsvProvider(tmp_path).get_bars("SPY", start=datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert df.index[0] >= pd.Timestamp("2026-02-01") and len(df) < 50
