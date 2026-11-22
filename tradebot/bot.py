"""The live paper-trading bot: fake money, real Binance prices, runs 24/7.

Every minute it checks prices and stop-losses. Every hour (when a new candle closes)
the autopilot re-decides what to hold. Everything is saved to data/bot.json, so
closing the window and reopening it continues where it left off.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import pandas as pd

from .autopilot import ACTIVE, COST_BPS, PLAYBOOKS, RISK, Brain, analyze
from .data import Binance, MarketData
from .portfolio import Portfolio, Trade

log = logging.getLogger("tradebot")
STATE_FILE = Path("data/bot.json")
DEFAULT = {"cash": 100.0, "risk": "normal", "coins": ["BTC", "ETH", "SOL"]}
POLL_SECONDS = 60
CANDLES = 24 * 21 + 120  # enough for the longest lookback + indicator warm-up


def now_utc() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


class Bot:
    def __init__(self, market: MarketData | None = None, path: Path = STATE_FILE):
        self.market = market or Binance()
        self.path = Path(path)
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None
        self.load()

    # ------------------------------------------------------------------ state
    def load(self) -> None:
        s = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.settings = {**DEFAULT, **s.get("settings", {})}
        self.portfolio = (Portfolio.from_dict(s["portfolio"]) if "portfolio" in s
                          else Portfolio(cash=self.settings["cash"], slippage_bps=COST_BPS))
        self.brain = Brain.from_dict(s.get("brain", {"risk": self.settings["risk"]}))
        self.events: list[dict] = s.get("events", [])
        self.equity: list[list] = s.get("equity", [])
        self.views: dict = s.get("views", {})
        self.last_decision: str | None = s.get("last_decision")
        self.started: str | None = s.get("started")
        self.want_running: bool = s.get("running", False)
        self.prices: dict = s.get("prices", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "settings": self.settings, "portfolio": self.portfolio.to_dict(), "brain": self.brain.to_dict(),
            "events": self.events[-300:], "equity": self.equity[-6000:], "views": self.views,
            "last_decision": self.last_decision, "started": self.started, "running": self.running,
            "prices": self.prices,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, default=str))
        tmp.replace(self.path)

    def event(self, kind: str, text: str, coin: str | None = None) -> None:
        self.events.append({"time": now_utc().isoformat(timespec="seconds"), "kind": kind, "coin": coin,
                            "text": text})
        log.info("%s", text)

    # ------------------------------------------------------------------ control
    def reset(self, cash: float | None = None, risk: str | None = None, coins: list[str] | None = None) -> None:
        was_running = self.running
        self.stop()  # outside the lock: the loop thread needs it to finish
        with self.lock:
            self.settings = {"cash": float(cash or self.settings["cash"]), "risk": risk or self.settings["risk"],
                             "coins": coins or self.settings["coins"]}
            self.portfolio = Portfolio(cash=self.settings["cash"], slippage_bps=COST_BPS)
            self.brain = Brain(self.settings["risk"])
            self.events, self.equity, self.views, self.last_decision, self.started = [], [], {}, None, None
            self.event("info", f"Fresh start with ${self.settings['cash']:,.2f} of play money.")
            self.save()
        if was_running:
            self.start()

    def update_settings(self, risk: str | None = None, coins: list[str] | None = None) -> None:
        with self.lock:
            if risk and risk != self.settings["risk"]:
                if risk not in RISK:
                    raise ValueError(f"Unknown risk level {risk}")
                self.settings["risk"] = self.brain.risk = risk
                self.event("info", f"Risk level set to {RISK[risk]['label']}. {RISK[risk]['blurb']}")
            if coins is not None:
                coins = [c.upper() for c in coins if c.strip()]
                if not coins:
                    raise ValueError("Pick at least one coin")
                if coins != self.settings["coins"]:
                    self.settings["coins"] = coins
                    self.event("info", f"Watching {', '.join(coins)}.")
            self.last_decision = None  # re-decide on the next check
            self.save()

    # ------------------------------------------------------------------ trading
    def tick(self, force_decision: bool = False) -> None:
        with self.lock:
            now = now_utc()
            coins = self.settings["coins"]
            held_outside = [c for c in self.portfolio.positions if c not in coins]
            prices = self.market.prices(coins + held_outside)
            self.prices = {**self.prices, **prices}
            cfg = RISK[self.settings["risk"]]

            # 1) stop-losses, every minute
            for c in list(self.portfolio.positions):
                if c in prices and prices[c] <= self.portfolio.avg_price[c] * (1 - cfg["stop"]):
                    t = self.portfolio.order(c, -self.portfolio.positions[c], prices[c], now.isoformat())
                    self.brain.on_stop(c, now)
                    self.event("sell", f"Stop-loss: sold {c} at ${t.price:,.4g} ({_money(t.realized_pnl)}). "
                                       f"Taking a {6}-hour break from {c}.", c)

            # 2) coins removed from the list get sold
            for c in held_outside:
                if c in prices:
                    t = self.portfolio.order(c, -self.portfolio.positions[c], prices[c], now.isoformat())
                    self.event("sell", f"Sold {c} (no longer on your coin list) {_money(t.realized_pnl)}.", c)

            # 3) hourly decision on the latest closed candle
            frames = {c: self.market.candles(c, CANDLES) for c in coins}
            latest = max(df.index[-1] for df in frames.values() if len(df))
            if force_decision or self.last_decision != latest.isoformat():
                self.decide(frames, prices, now)
                self.last_decision = latest.isoformat()

            for c, v in self.views.items():
                if c in prices:
                    v["price"] = prices[c]
            eq = self.portfolio.current_equity(self.prices)
            if not self.equity or now - pd.Timestamp(self.equity[-1][0]) >= pd.Timedelta(minutes=5):
                self.equity.append([now.isoformat(timespec="seconds"), eq])
            self.save()

    def decide(self, frames: dict, prices: dict, now: pd.Timestamp) -> None:
        cfg = RISK[self.settings["risk"]]
        rows = {}
        for c, df in frames.items():
            if len(df) < 60:
                continue
            an = analyze(df, cfg["lookback_days"])
            rows[c] = an.iloc[-1].to_dict()
        held = set(self.portfolio.positions)
        targets, decisions = self.brain.step(rows, held, now)
        before = dict(self.portfolio.positions)
        trades = self.portfolio.rebalance({c: targets[c] for c in rows if c in prices},
                                          prices, now.isoformat(timespec="seconds"),
                                          weights={c: cfg["per_coin"] for c in rows}, max_leverage=1.0)
        by_coin = {d.coin: d for d in decisions}
        for t in trades:
            d = by_coin.get(t.symbol)
            why = f" Why: {d.reason}" if d else ""
            if t.qty > 0:
                self.event("buy", f"Bought {t.symbol} for ${abs(t.qty) * t.price:,.2f} at ${t.price:,.4g}.{why}",
                           t.symbol)
            else:
                self.event("sell", f"Sold {t.symbol} at ${t.price:,.4g} ({_money(t.realized_pnl)}).{why}", t.symbol)
        if not trades and not before and not any(targets.values()):
            if not self.events or self.events[-1]["kind"] != "wait":
                self.event("wait", "Checked the market: nothing worth buying right now, staying in cash.")
        self.views = {}
        for c, d in by_coin.items():
            df = frames[c]
            self.views[c] = {
                **d.to_dict(), "price": prices.get(c),
                "change_24h": float(df["close"].iloc[-1] / df["close"].iloc[-25] - 1) if len(df) > 25 else None,
                "spark": [float(x) for x in df["close"].iloc[-72:].to_numpy()],
                "playbooks": {p: {"label": PLAYBOOKS[p]["label"], "perf": d.perf.get(p)} for p in ACTIVE},
            }

    # ------------------------------------------------------------------ loop
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        if not self.started:
            self.started = now_utc().isoformat(timespec="seconds")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="bot", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._thread = None

    def _loop(self) -> None:
        self.event("info", "Bot started. It checks prices every minute and re-decides every hour.")
        first = True
        while not self._stop.is_set():
            try:
                self.tick(force_decision=first)
                self.last_error = None
                first = False
            except Exception as e:
                self.last_error = str(e)
                log.error("Check failed: %s", e)
            self._stop.wait(POLL_SECONDS)
        with self.lock:
            self.event("info", "Bot paused.")
            self.save()

    # ------------------------------------------------------------------ view
    def snapshot(self) -> dict:
        with self.lock:
            eq = self.portfolio.current_equity(self.prices)
            start_cash = self.settings["cash"]
            day_ago = now_utc() - pd.Timedelta(hours=24)
            past = [v for t, v in self.equity if pd.Timestamp(t) <= day_ago]
            base_24h = past[-1] if past else (self.equity[0][1] if self.equity else start_cash)
            holdings = []
            for c, q in self.portfolio.positions.items():
                price = self.prices.get(c, self.portfolio.avg_price[c])
                holdings.append({"coin": c, "qty": q, "value": q * price, "entry": self.portfolio.avg_price[c],
                                 "price": price, "pnl": price / self.portfolio.avg_price[c] - 1,
                                 "stop": self.portfolio.avg_price[c] * (1 - RISK[self.settings["risk"]]["stop"])})
            closed = [t.realized_pnl for t in self.portfolio.trades if t.realized_pnl is not None]
            return {
                "running": self.running, "error": self.last_error, "settings": self.settings,
                "risk_levels": {k: {"label": v["label"], "blurb": v["blurb"]} for k, v in RISK.items()},
                "balance": eq, "cash": self.portfolio.cash, "start_cash": start_cash,
                "total_return": eq / start_cash - 1, "change_24h": eq / base_24h - 1 if base_24h else 0,
                "holdings": holdings, "views": self.views, "events": list(reversed(self.events[-80:])),
                "equity": self.equity, "started": self.started, "last_decision": self.last_decision,
                "trades": len(closed), "wins": sum(p > 0 for p in closed),
            }


def _money(x) -> str:
    return "" if x is None else (f"+${x:,.2f}" if x >= 0 else f"-${-x:,.2f}")


__all__ = ["Bot", "Trade"]
