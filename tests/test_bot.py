import pandas as pd

from tradebot.bot import Bot


def test_tick_decides_trades_and_persists(market, tmp_path):
    path = tmp_path / "bot.json"
    bot = Bot(market=market, path=path)
    bot.reset(cash=100, risk="aggressive", coins=["BTC", "ETH", "SOL", "DOGE"])
    bot.tick(force_decision=True)
    snap = bot.snapshot()
    assert set(snap["views"]) == {"BTC", "ETH", "SOL", "DOGE"}
    assert all(v["reason"] for v in snap["views"].values())
    assert len(snap["holdings"]) <= 1  # aggressive = one coin max
    assert snap["balance"] > 0 and snap["events"]

    again = Bot(market=market, path=path)  # survives a restart
    assert again.portfolio.positions == bot.portfolio.positions
    assert again.settings["risk"] == "aggressive"


def test_stop_loss_sells(market, tmp_path):
    bot = Bot(market=market, path=tmp_path / "b.json")
    bot.reset(cash=100, risk="normal", coins=["BTC"])
    price = market.prices(["BTC"])["BTC"]
    bot.portfolio.order("BTC", 0.5 * 100 / price, price, "t")  # pretend we bought
    market.price_bump["BTC"] = 0.9  # price drops 10% (stop is 6%)
    bot.tick()
    assert "BTC" not in bot.portfolio.positions
    assert any("Stop-loss" in e["text"] for e in bot.events)
    assert pd.Timestamp(bot.brain.cooldown_until["BTC"]) > pd.Timestamp.now(tz="UTC")


def test_removed_coin_is_sold(market, tmp_path):
    bot = Bot(market=market, path=tmp_path / "b.json")
    bot.reset(cash=100, coins=["BTC", "ETH"])
    price = market.prices(["ETH"])["ETH"]
    bot.portfolio.order("ETH", 10 / price, price, "t")
    bot.update_settings(coins=["BTC"])
    bot.tick()
    assert "ETH" not in bot.portfolio.positions


def test_start_stop_thread(market, tmp_path, monkeypatch):
    import tradebot.bot as botmod

    monkeypatch.setattr(botmod, "POLL_SECONDS", 0.05)
    bot = Bot(market=market, path=tmp_path / "b.json")
    bot.start()
    assert bot.running
    import time
    time.sleep(0.3)
    bot.stop()
    assert not bot.running and bot.last_decision


def test_restart_shutdown_keeps_running_flag(market, tmp_path, monkeypatch):
    import tradebot.bot as botmod

    monkeypatch.setattr(botmod, "POLL_SECONDS", 0.05)
    path = tmp_path / "b.json"
    bot = Bot(market=market, path=path)
    bot.start()
    bot.stop(pause=False)  # what the updater does before restarting
    bot.save()
    assert Bot(market=market, path=path).want_running is True
    bot.start()
    bot.stop()  # the user pressing Pause
    assert Bot(market=market, path=path).want_running is False
