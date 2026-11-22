import pandas as pd
import pytest

from tradebot.autopilot import ACTIVE, RISK, Brain, analyze, playbook_signals
from tradebot.data import Synthetic


@pytest.fixture
def df():
    return Synthetic(hours=24 * 90, seed=3).candles("BTC", 24 * 90)


def test_signals_never_look_ahead(df):
    full = playbook_signals(df)
    for cut in (500, 1200, len(df) - 1):
        pd.testing.assert_frame_equal(playbook_signals(df.iloc[:cut]), full.iloc[:cut])


def test_analysis_never_looks_ahead(df):
    full = analyze(df, 14)
    cut = 1500
    pd.testing.assert_frame_equal(analyze(df.iloc[:cut], 14), full.iloc[:cut])


def test_all_playbooks_trade(df):
    sigs = playbook_signals(df)
    for p in ACTIVE:
        assert sigs[p].sum() > 0, p


def row(scores, perfs, sigs):
    r = {}
    for p in ACTIVE:
        r[f"score_{p}"], r[f"perf_{p}"], r[f"sig_{p}"] = scores[p], perfs[p], sigs[p]
    r.update(efficiency=0.4, change_48h=0.05)
    return r


def test_picks_best_playbook_and_cash_when_nothing_works():
    b = Brain("normal")
    d = b.pick("BTC", row({"trend": 2, "breakout": 1, "dip": 0.2}, {"trend": .05, "breakout": .02, "dip": 0}, {"trend": 1, "breakout": 0, "dip": 0}))
    assert d.playbook == "trend" and d.signal == 1 and d.mood == "Trending up"
    d = b.pick("ETH", row({"trend": .1, "breakout": -1, "dip": .3}, {"trend": .001, "breakout": -.03, "dip": .002}, {"trend": 1, "breakout": 1, "dip": 1}))
    assert d.playbook == "cash" and d.signal == 0


def test_sticky_choice_avoids_flip_flopping():
    b = Brain("normal", current={"BTC": "breakout"})
    d = b.pick("BTC", row({"trend": 1.2, "breakout": 1.0, "dip": 0}, {"trend": .04, "breakout": .03, "dip": 0}, {"trend": 1, "breakout": 1, "dip": 0}))
    assert d.playbook == "breakout"


def test_step_limits_coins_by_risk_and_explains():
    now = pd.Timestamp("2026-01-01", tz="UTC")
    rows = {c: row({"trend": s, "breakout": 0, "dip": 0}, {"trend": .05, "breakout": 0, "dip": 0}, {"trend": 1, "breakout": 0, "dip": 0})
            for c, s in {"BTC": 3, "ETH": 2, "SOL": 1}.items()}
    targets, decisions = Brain("aggressive").step(rows, set(), now)
    assert targets == {"BTC": 1, "ETH": 0, "SOL": 0}
    assert {d.coin: d.action for d in decisions} == {"BTC": "buy", "ETH": "skip", "SOL": "skip"}
    assert all(d.reason for d in decisions)
    targets, _ = Brain("safe").step(rows, set(), now)
    assert sum(targets.values()) == RISK["safe"]["max_coins"]
    # already holding SOL -> kept ahead of new candidates
    targets, _ = Brain("aggressive").step(rows, {"SOL"}, now)
    assert targets["SOL"] == 1 and targets["BTC"] == 0


def test_cooldown_after_stop():
    now = pd.Timestamp("2026-01-01", tz="UTC")
    b = Brain("aggressive")
    b.on_stop("BTC", now)
    rows = {"BTC": row({"trend": 3, "breakout": 0, "dip": 0}, {"trend": .05, "breakout": 0, "dip": 0}, {"trend": 1, "breakout": 0, "dip": 0})}
    assert b.step(rows, set(), now + pd.Timedelta(hours=1))[0]["BTC"] == 0
    assert b.step(rows, set(), now + pd.Timedelta(hours=7))[0]["BTC"] == 1
