"""YAML config for paper-trading instances."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from .strategies import strategy_class


@dataclass
class InstanceConfig:
    name: str
    strategy: str
    symbols: list[str]
    params: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    data_source: str = "yfinance"
    interval: str = "1d"
    starting_cash: float = 10_000.0
    commission: float = 0.0
    slippage_bps: float = 1.0
    fractional: bool = True
    allocation: float = 1.0  # fraction of equity to deploy across symbols
    max_leverage: float = 1.0  # cap on total exposure / equity (2 = 2x margin)
    broker: str = "sim"  # "sim" = local simulated account, "alpaca" = Alpaca paper account
    alpaca_account: str = "default"  # which ALPACA_<ACCOUNT>_API_KEY to use (broker: alpaca)
    market_hours_only: bool = True
    # Daily/weekly bars only: act once the clock passes this New York time, so
    # decisions use (almost) the final daily bar, like the backtester does.
    daily_trade_after: str = "15:45"
    lookback_days: int | None = None  # history to fetch; default depends on interval


@dataclass
class Config:
    instances: list[InstanceConfig]
    poll_seconds: int = 60
    state_dir: str = "state"

    def get(self, name: str) -> InstanceConfig:
        for inst in self.instances:
            if inst.name == name:
                return inst
        raise KeyError(f"No instance named '{name}'. Have: {[i.name for i in self.instances]}")


_INSTANCE_FIELDS = {f.name for f in fields(InstanceConfig)}
_GLOBAL_FIELDS = {"poll_seconds", "state_dir"}


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    defaults = raw.get("defaults", {}) or {}
    unknown = set(defaults) - _INSTANCE_FIELDS - _GLOBAL_FIELDS
    if unknown:
        raise ValueError(f"Unknown keys in defaults: {sorted(unknown)}")

    instances, seen = [], set()
    for item in raw.get("instances", []) or []:
        if not isinstance(item.get("name"), str):
            raise ValueError(f"Instance name {item.get('name')!r} must be a string (quote it, e.g. \"off\")")
        # precedence: built-in < `defaults:` < what the strategy needs < the instance itself
        merged = {k: v for k, v in defaults.items() if k in _INSTANCE_FIELDS}
        if "strategy" not in item:
            raise ValueError(f"Instance {item['name']!r} has no strategy")
        cls = strategy_class(item["strategy"])
        if cls.interval:
            merged["interval"] = cls.interval
        merged.update({k: v for k, v in cls.recommended.items() if k in _INSTANCE_FIELDS})
        merged.update(item)
        if not merged.get("symbols"):
            raise ValueError(f"Instance {item['name']!r} needs symbols")
        unknown = set(merged) - _INSTANCE_FIELDS
        if unknown:
            raise ValueError(f"Unknown keys in instance {item.get('name')!r}: {sorted(unknown)}")
        if isinstance(merged.get("symbols"), str):
            merged["symbols"] = [merged["symbols"]]
        merged["symbols"] = [s.upper() for s in merged["symbols"]]
        inst = InstanceConfig(**merged)
        if inst.broker not in ("sim", "alpaca"):
            raise ValueError(f"Instance {inst.name!r}: broker must be 'sim' or 'alpaca'")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", inst.name):
            raise ValueError(f"Instance name {inst.name!r} may only use letters, digits, _ . -")
        if inst.name in seen:
            raise ValueError(f"Duplicate instance name {inst.name!r}")
        seen.add(inst.name)
        instances.append(inst)

    accounts: dict[str, str] = {}
    for inst in instances:
        if inst.broker == "alpaca" and inst.enabled:
            other = accounts.setdefault(inst.alpaca_account, inst.name)
            if other != inst.name:
                raise ValueError(
                    f"Instances {other!r} and {inst.name!r} both use Alpaca account "
                    f"{inst.alpaca_account!r}. Each Alpaca instance needs its own paper account "
                    "(set a different alpaca_account and add its keys to .env)."
                )

    return Config(
        instances=instances,
        poll_seconds=int(defaults.get("poll_seconds", 60)),
        state_dir=str(defaults.get("state_dir", "state")),
    )
