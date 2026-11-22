"""Strategy interface and registry.

A strategy turns a DataFrame of OHLCV bars into a *target position* per bar:

    1  -> be long
    0  -> be flat
   -1  -> be short (only if the strategy wants shorts)

The value at bar ``t`` may only use information available at the close of bar
``t``. The backtester acts on it at the open of bar ``t+1``; the paper trader
acts on the latest bar's value at the current price.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

import numpy as np
import pandas as pd

_REGISTRY: dict[str, type["Strategy"]] = {}


class Strategy(ABC):
    #: Unique key used in configs and on the command line.
    name: ClassVar[str] = ""
    #: One-line human description.
    description: ClassVar[str] = ""
    #: Where the idea came from ("classic", "tiktok @someone", a URL, ...).
    source: ClassVar[str] = ""
    #: Default parameters. Any can be overridden per instance / per backtest.
    default_params: ClassVar[dict[str, Any]] = {}
    #: Bar size the strategy is designed for. ``None`` = works on any bar size
    #: (defaults to daily). Set it (e.g. "5m") if the rules only make sense there.
    interval: ClassVar[str | None] = None
    #: Settings the bot should use for this strategy unless you override them in
    #: the config / on the command line, e.g. {"symbols": ["SPY"], "max_leverage": 2}.
    #: Keys: any instance config key (symbols, max_leverage, allocation, ...).
    recommended: ClassVar[dict[str, Any]] = {}
    #: Free text shown by ``tradebot info``: assumptions, how to read it, caveats.
    notes: ClassVar[str] = ""
    #: Friendly name shown in the dashboard, e.g. "Buy the Dip (RSI)".
    display_name: ClassVar[str] = ""
    #: Beginner explanation shown by the dashboard's info button. Keys (all optional):
    #: summary (one line), how, good (when it works), bad (when it struggles), sizing, caveat.
    explainer: ClassVar[dict[str, str]] = {}
    #: One line per parameter explaining what changing it does.
    param_help: ClassVar[dict[str, str]] = {}
    #: Group shown in the dashboard: "classic", "social" or "day trading".
    category: ClassVar[str] = ""

    def __init__(self, **params: Any) -> None:
        unknown = set(params) - set(self.default_params)
        if unknown:
            raise ValueError(
                f"Unknown parameter(s) for strategy '{self.name}': {sorted(unknown)}. "
                f"Valid: {sorted(self.default_params)}"
            )
        self.params: dict[str, Any] = {**self.default_params, **params}

    def __getattr__(self, item: str) -> Any:
        # Lets strategies write ``self.fast`` instead of ``self.params["fast"]``.
        params = self.__dict__.get("params", {})
        if item in params:
            return params[item]
        raise AttributeError(item)

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.Series | pd.DataFrame:
        """Return the target position for every bar.

        ``df`` has lowercase columns: open, high, low, close, volume.

        Return either
          * a Series with values in {-1, 0, 1}  (position size is then decided by
            the bot: equity split equally across the instance's symbols), or
          * a DataFrame with columns ``signal`` ({-1, 0, 1}) and ``weight``
            (fraction of equity to put in this position when it is opened, e.g.
            0.5 = half the account). Use this for risk-based sizing.
        """

    def targets(self, df: pd.DataFrame) -> pd.DataFrame:
        """Validated wrapper: DataFrame with ``signal`` (int) and ``weight`` (float or NaN)."""
        out = self.generate_signals(df)
        if isinstance(out, pd.DataFrame):
            sig, weight = out["signal"], out.get("weight")
        else:
            sig, weight = out, None
        if not isinstance(sig, pd.Series):
            sig = pd.Series(sig, index=df.index)
        sig = np.sign(sig.reindex(df.index).astype(float).fillna(0.0)).astype(int)
        if weight is None:
            weight = pd.Series(np.nan, index=df.index)
        weight = weight.reindex(df.index).astype(float).clip(lower=0)
        return pd.DataFrame({"signal": sig, "weight": weight}, index=df.index)

    def signals(self, df: pd.DataFrame) -> pd.Series:
        return self.targets(df)["signal"]

    def __repr__(self) -> str:
        args = ", ".join(f"{k}={v!r}" for k, v in self.params.items())
        return f"{self.name}({args})"


def register(cls: type[Strategy]) -> type[Strategy]:
    """Class decorator that makes a strategy available by its ``name``."""
    if not cls.name:
        raise ValueError(f"{cls.__name__} must set a 'name'")
    if cls.name in _REGISTRY and _REGISTRY[cls.name] is not cls:
        raise ValueError(f"Duplicate strategy name '{cls.name}'")
    _REGISTRY[cls.name] = cls
    return cls


def latch(entries: pd.Series, exits: pd.Series, value: int = 1) -> pd.Series:
    """Hold ``value`` from an entry bar until an exit bar, else 0.

    Turns "enter when X, exit when Y" rules into a position series.
    If entry and exit fire on the same bar, the exit wins.
    """
    state = pd.Series(np.nan, index=entries.index)
    state[entries.fillna(False).astype(bool)] = value
    state[exits.fillna(False).astype(bool)] = 0
    return state.ffill().fillna(0).astype(int)
