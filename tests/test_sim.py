import pytest

from tradebot.sim import simulate


@pytest.mark.parametrize("risk", ["safe", "normal", "aggressive"])
def test_simulation_runs_and_respects_risk(market, risk):
    r = simulate(market, ["BTC", "ETH", "SOL"], days=60, risk=risk, cash=100)
    assert r["end"] > 0 and r["trades"] > 0
    assert -1 < r["max_drawdown"] <= 0
    assert len(r["curve"]) > 50 and r["curve"][0][1] == pytest.approx(100, rel=0.02)
    assert abs(sum(r["playbook_share"].values()) - 1) < 1e-9


def test_fees_cost_money(market, monkeypatch):
    import tradebot.sim as sim

    with_fees = simulate(market, ["BTC", "ETH"], days=60, cash=100)
    monkeypatch.setattr(sim, "COST_BPS", 0.0)
    free = simulate(market, ["BTC", "ETH"], days=60, cash=100)
    assert free["end"] >= with_fees["end"]
