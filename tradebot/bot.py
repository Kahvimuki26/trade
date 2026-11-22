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
DEFAULT = {"cash": 100.0, "risk": "normal", "coins": ["BTC", "ETH", "SOL"], "ai_every_hours": 1}
KINDS = {"autopilot": "Autopilot", "claude": "Claude AI"}
POLL_SECONDS = 60
CANDLES = 24 * 21 + 120  # enough for the longest lookback + indicator warm-up


def now_utc() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


class Bot:
    def __init__(self, market: MarketData | None = None, path: Path = STATE_FILE, kind: str = "autopilot",
                 trader=None):
        self.market = market or Binance()
        self.path = Path(path)
        self.kind = kind
        if kind == "claude":
            from .ai import ClaudeTrader

            self.trader = trader or ClaudeTrader()
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
        self.ai = s.get("ai", {"memory": {}, "cost": 0.0, "view": None, "last": None, "calls": 0})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "settings": self.settings, "portfolio": self.portfolio.to_dict(), "brain": self.brain.to_dict(),
            "events": self.events[-300:], "equity": self.equity[-6000:], "views": self.views,
            "last_decision": self.last_decision, "started": self.started, "running": self.want_running,
            "prices": self.prices, "ai": self.ai,
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
            self.ai = {"memory": {}, "cost": self.ai.get("cost", 0.0), "view": None, "last": None,
                       "calls": self.ai.get("calls", 0)}  # keep the running API cost total
            self.event("info", f"Fresh start with ${self.settings['cash']:,.2f} of play money.")
            self.save()
        if was_running:
            self.start()

    def update_settings(self, risk: str | None = None, coins: list[str] | None = None,
                        ai_every_hours: int | None = None) -> None:
        with self.lock:
            if ai_every_hours is not None:
                if int(ai_every_hours) not in (1, 2, 4, 8):
                    raise ValueError("Pick 1, 2, 4 or 8 hours")
                self.settings["ai_every_hours"] = int(ai_every_hours)
                self.event("info", f"Claude now decides every {int(ai_every_hours)} hour(s).")
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
            if (force_decision or self.last_decision != latest.isoformat()) and self._ai_due(now, force_decision):
                self.last_decision = latest.isoformat()
                if self.kind == "claude":
                    try:
                        self.decide_ai(frames, prices, now)
                    except Exception as e:  # keep positions, stops keep working, try again next hour
                        self.ai["last"] = now.isoformat(timespec="seconds")
                        self.event("error", f"Claude couldn't decide this time: {_explain(e)}")
                        raise
                else:
                    self.decide(frames, prices, now)

            for c, v in self.views.items():
                if c in prices:
                    v["price"] = prices[c]
            eq = self.portfolio.current_equity(self.prices)
            if not self.equity or now - pd.Timestamp(self.equity[-1][0]) >= pd.Timedelta(minutes=5):
                self.equity.append([now.isoformat(timespec="seconds"), eq])
            self.save()

    def _ai_due(self, now: pd.Timestamp, force: bool) -> bool:
        if self.kind != "claude" or force or not self.ai.get("last"):
            return True
        return now - pd.Timestamp(self.ai["last"]) >= pd.Timedelta(hours=self.settings["ai_every_hours"],
                                                                    minutes=-5)

    def decide_ai(self, frames: dict, prices: dict, now: pd.Timestamp) -> None:
        from .ai import to_targets
        from .autopilot import mood

        cfg = RISK[self.settings["risk"]]
        coins = [c for c, df in frames.items() if len(df) >= 210 and c in prices]
        eq = self.portfolio.current_equity(self.prices)
        positions = [{"coin": c, "value_usd": round(q * prices.get(c, self.portfolio.avg_price[c]), 2),
                      "entry_price": self.portfolio.avg_price[c],
                      "pnl_pct": round((prices.get(c, 0) / self.portfolio.avg_price[c] - 1) * 100, 2)}
                     for c, q in self.portfolio.positions.items()]
        closed = [t.realized_pnl for t in self.portfolio.trades if t.realized_pnl is not None]
        account = {"total_value_usd": round(eq, 2), "cash_usd": round(self.portfolio.cash, 2),
                   "start_value_usd": self.settings["cash"],
                   "return_since_start_pct": round((eq / self.settings["cash"] - 1) * 100, 2),
                   "closed_trades": len(closed), "winning_trades": sum(p > 0 for p in closed),
                   "positions": positions}
        data = self.trader.decide({c: frames[c] for c in coins}, account, self.settings["risk"],
                                  self.ai.get("memory", {}))
        self.ai["cost"] = self.ai.get("cost", 0.0) + data["cost"]
        self.ai["calls"] = self.ai.get("calls", 0) + 1
        self.ai["last"] = now.isoformat(timespec="seconds")
        self.ai["view"] = data.get("market_view")

        held = set(self.portfolio.positions)
        targets, weights, decisions = to_targets(data, coins, held, self.settings["risk"])
        cooling = {c for c, t in self.brain.cooldown_until.items() if pd.Timestamp(t) > now}
        for d in decisions:
            if d["coin"] in cooling and d["coin"] not in held and targets.get(d["coin"]):
                targets[d["coin"]], d["action"] = 0, "stay_out"
                d["reason"] += " (Blocked: this coin hit its stop-loss recently, 6-hour break.)"
        trades = self.portfolio.rebalance(targets, prices, now.isoformat(timespec="seconds"),
                                          weights=weights, max_leverage=1.0)
        by_coin = {d["coin"]: d for d in decisions}
        for t in trades:
            d = by_coin.get(t.symbol, {})
            why = f" Claude: \"{d.get('reason', '')}\"" if d else ""
            if t.qty > 0:
                self.event("buy", f"Bought {t.symbol} for ${abs(t.qty) * t.price:,.2f} at ${t.price:,.4g}.{why}",
                           t.symbol)
            else:
                self.event("sell", f"Sold {t.symbol} at ${t.price:,.4g} ({_money(t.realized_pnl)}).{why}", t.symbol)
        if not trades:
            self.event("wait", f"Claude checked the market: {data.get('market_view', '')} No trades this time.")
        mem = self.ai.setdefault("memory", {})
        mem["note"] = data.get("note_to_self", "")
        mem.setdefault("recent", []).append({
            "time": now.strftime("%m-%d %H:%M"),
            "did": ", ".join(f"{d['coin']} {d['action']}" + (f" {d['size_pct']:.0f}%" if d["action"] == "buy" else "")
                             for d in decisions)})
        mem["recent"] = mem["recent"][-12:]
        self.views = {}
        for c in coins:
            df, d = frames[c], by_coin[c]
            action = {"buy": "buy", "sell": "sell", "hold": "hold", "stay_out": "wait"}[d["action"]]
            self.views[c] = {
                "coin": c, "playbook": "claude", "label": f"Claude: {d['action'].replace('_', ' ')}",
                "action": action, "confidence": d.get("confidence"), "reason": d["reason"],
                "mood": mood(analyze(df.iloc[-200:], 7).iloc[-1].to_dict()), "price": prices.get(c),
                "change_24h": float(df["close"].iloc[-1] / df["close"].iloc[-25] - 1) if len(df) > 25 else None,
                "spark": [float(x) for x in df["close"].iloc[-72:].to_numpy()],
            }

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
        self.want_running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="bot", daemon=True)
        self._thread.start()

    def stop(self, pause: bool = True) -> None:
        """pause=False: shutting down for a restart, so it resumes when the app starts again."""
        if pause:
            self.want_running = False
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._thread = None

    def _loop(self) -> None:
        if self.kind == "claude":
            self.event("info", f"Claude started. Stop-losses are checked every minute; Claude decides every "
                               f"{self.settings['ai_every_hours']} hour(s).")
        else:
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
            if not self.want_running:
                self.event("info", "Bot paused.")
            self.save()

    # ------------------------------------------------------------------ view
    def _ai_configured(self) -> bool:
        from .ai import ClaudeTrader

        return ClaudeTrader.configured() or getattr(self.trader, "_client", None) is not None

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
                "kind": self.kind, "name": KINDS[self.kind],
                "ai": {"cost": self.ai.get("cost", 0.0), "calls": self.ai.get("calls", 0), "view": self.ai.get("view"),
                       "last": self.ai.get("last"), "configured": self._ai_configured()} if self.kind == "claude" else None,
            }


def _explain(e: Exception) -> str:
    name = type(e).__name__
    if name == "AuthenticationError":
        return "the Anthropic API key is missing or wrong (check ANTHROPIC_API_KEY in .env)."
    if name == "PermissionDeniedError":
        return "the API key isn't allowed to use this model."
    if name == "RateLimitError":
        return "too many requests right now (rate limit). It'll try again next time."
    if name in ("APIConnectionError", "APITimeoutError"):
        return "couldn't reach Anthropic's servers."
    if "credit balance" in str(e).lower():
        return "your Anthropic account is out of credit (add some at console.anthropic.com)."
    return str(e)


def _money(x) -> str:
    return "" if x is None else (f"+${x:,.2f}" if x >= 0 else f"-${-x:,.2f}")


__all__ = ["Bot", "Trade"]
