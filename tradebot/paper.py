"""Paper trading: run many strategy instances side by side on live data.

Each instance has its own simulated account saved to ``<state_dir>/<name>.json``,
so you can stop and restart the bot without losing positions or history.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd

from .config import Config, InstanceConfig
from .data import DataProvider, default_lookback, get_provider, parse_interval, utcnow
from .portfolio import Portfolio, Trade
from .strategies import get_strategy

log = logging.getLogger("tradebot")
NY = ZoneInfo("America/New_York")

Fetch = Callable[[InstanceConfig, str], pd.DataFrame]


def market_is_open(now: datetime) -> bool:
    """Regular US session, Mon to Fri 9:30-16:00 ET. Exchange holidays are not handled."""
    ny = now.astimezone(NY)
    return ny.weekday() < 5 and dtime(9, 30) <= ny.time() < dtime(16, 0)


def drop_incomplete_bar(df: pd.DataFrame, interval: str, now: datetime) -> pd.DataFrame:
    """For intraday bars, drop the last bar if it hasn't closed yet."""
    n, unit = parse_interval(interval)
    if unit not in ("m", "h") or df.empty:
        return df
    step = timedelta(minutes=n) if unit == "m" else timedelta(hours=n)
    last = df.index[-1]
    last = last.tz_localize(NY) if last.tzinfo is None else last
    return df.iloc[:-1] if last + step > now else df


class PaperInstance:
    def __init__(self, cfg: InstanceConfig, state_dir: str | Path = "state"):
        self.cfg = cfg
        self.strategy = get_strategy(cfg.strategy, **cfg.params)
        self.path = Path(state_dir) / f"{cfg.name}.json"
        self.equity_history: list[tuple[str, float]] = []
        self.last_signals: dict[str, int] = {}
        self.last_prices: dict[str, float] = {}
        self.created = utcnow().isoformat()
        if self.path.exists():
            self._load()
        else:
            self.portfolio = Portfolio(
                cash=cfg.starting_cash,
                commission=cfg.commission,
                slippage_bps=cfg.slippage_bps,
                fractional=cfg.fractional,
            )

    # ---------------------------------------------------------------- state
    def _load(self) -> None:
        s = json.loads(self.path.read_text())
        self.portfolio = Portfolio.from_dict(s["portfolio"])
        self.equity_history = [tuple(x) for x in s.get("equity_history", [])]
        self.last_signals = s.get("last_signals", {})
        self.last_prices = s.get("last_prices", {})
        self.created = s.get("created", self.created)
        if s.get("strategy") != self.cfg.strategy or s.get("params") != self.strategy.params:
            log.warning(
                "[%s] config changed since this state was created (%s %s -> %s %s). "
                "Continuing with existing positions. Run `tradebot reset %s` for a clean start.",
                self.cfg.name, s.get("strategy"), s.get("params"),
                self.cfg.strategy, self.strategy.params, self.cfg.name,
            )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "name": self.cfg.name,
            "strategy": self.cfg.strategy,
            "params": self.strategy.params,
            "symbols": self.cfg.symbols,
            "created": self.created,
            "starting_cash": self.cfg.starting_cash,
            "last_signals": self.last_signals,
            "last_prices": self.last_prices,
            "equity_history": self.equity_history,
            "portfolio": self.portfolio.to_dict(),
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1, default=str))
        tmp.replace(self.path)

    # ---------------------------------------------------------------- trading
    def should_trade_now(self, now: datetime) -> bool:
        if self.cfg.market_hours_only and not market_is_open(now):
            return False
        _, unit = parse_interval(self.cfg.interval)
        if unit in ("d", "wk") and self.cfg.market_hours_only:
            hh, mm = map(int, self.cfg.daily_trade_after.split(":"))
            return now.astimezone(NY).time() >= dtime(hh, mm)
        return True

    def tick(self, fetch: Fetch, now: datetime | None = None) -> list[Trade]:
        now = now or utcnow()
        targets, prices = {}, {}
        for sym in self.cfg.symbols:
            raw = fetch(self.cfg, sym)
            prices[sym] = float(raw["close"].iloc[-1])  # latest price, incl. live bar
            bars = drop_incomplete_bar(raw, self.cfg.interval, now)
            sig = self.strategy.signals(bars)
            targets[sym] = int(sig.iloc[-1]) if len(sig) else 0

        trades = self.portfolio.rebalance(targets, prices, now.isoformat(timespec="seconds"),
                                          allocation=self.cfg.allocation)
        for t in trades:
            pnl = f"  pnl {t.realized_pnl:+,.2f}" if t.realized_pnl is not None else ""
            log.info("[%s] %s %.4f %s @ %.2f%s", self.cfg.name, t.side, abs(t.qty), t.symbol, t.price, pnl)

        self.last_signals, self.last_prices = targets, prices
        all_prices = {**{s: self.portfolio.avg_price[s] for s in self.portfolio.positions}, **prices}
        self.equity_history.append((now.isoformat(timespec="seconds"), self.portfolio.equity(all_prices)))
        self.save()
        return trades


def make_fetcher(providers: dict[str, DataProvider] | None = None, now: datetime | None = None) -> Fetch:
    """Fetch function that caches per (source, symbol, interval) for one tick."""
    providers = providers if providers is not None else {}
    cache: dict[tuple, pd.DataFrame] = {}
    now = now or utcnow()

    def fetch(cfg: InstanceConfig, symbol: str) -> pd.DataFrame:
        key = (cfg.data_source, symbol, cfg.interval)
        if key not in cache:
            if cfg.data_source not in providers:
                providers[cfg.data_source] = get_provider(cfg.data_source)
            lookback = timedelta(days=cfg.lookback_days) if cfg.lookback_days else default_lookback(cfg.interval)
            cache[key] = providers[cfg.data_source].get_bars(symbol, start=now - lookback, interval=cfg.interval)
        return cache[key]

    return fetch


def run(
    config: Config,
    names: list[str] | None = None,
    once: bool = False,
    force: bool = False,
    providers: dict[str, DataProvider] | None = None,
) -> None:
    """Main loop. ``force`` ignores market hours / trade windows (useful for testing)."""
    selected = [i for i in config.instances if i.enabled and (not names or i.name in names)]
    if names:
        missing = set(names) - {i.name for i in selected}
        if missing:
            raise KeyError(f"Unknown or disabled instance(s): {sorted(missing)}")
    if not selected:
        raise ValueError("No enabled instances to run")

    instances = [PaperInstance(c, config.state_dir) for c in selected]
    providers = providers if providers is not None else {}
    log.info("Paper trading %d instance(s): %s", len(instances),
             ", ".join(f"{i.cfg.name} ({i.strategy!r} on {','.join(i.cfg.symbols)})" for i in instances))

    waiting_logged = False
    while True:
        now = utcnow()
        due = [i for i in instances if force or i.should_trade_now(now)]
        if due:
            waiting_logged = False
            fetch = make_fetcher(providers, now)
            for inst in due:
                try:
                    inst.tick(fetch, now)
                except Exception as e:  # keep the other instances running
                    log.error("[%s] tick failed: %s", inst.cfg.name, e)
        elif not waiting_logged:
            log.info("Nothing to do right now (market closed or outside trade window). Waiting...")
            waiting_logged = True

        if once:
            return
        time.sleep(config.poll_seconds)
