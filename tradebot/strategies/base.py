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
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        """Return a Series aligned to ``df.index`` with values in {-1, 0, 1}.

        ``df`` has lowercase columns: open, high, low, close, volume.
        """

    def signals(self, df: pd.DataFrame) -> pd.Series:
        """Validated wrapper around :meth:`generate_signals`."""
        sig = self.generate_signals(df)
        if not isinstance(sig, pd.Series):
            sig = pd.Series(sig, index=df.index)
        sig = sig.reindex(df.index).astype(float).fillna(0.0)
        return np.sign(sig).astype(int)

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
