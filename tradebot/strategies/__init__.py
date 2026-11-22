"""Strategy registry.

Every module in this package (including sub-packages such as ``classic`` and
``social``) is imported automatically, so adding a strategy is just dropping a
new file in here. Files whose name starts with ``_`` are skipped.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

from .base import _REGISTRY, Strategy, latch, register

__all__ = ["Strategy", "register", "latch", "get_strategy", "list_strategies", "strategy_class"]

_loaded = False


def _autoload() -> None:
    global _loaded
    if _loaded:
        return
    for mod in pkgutil.walk_packages(__path__, prefix=f"{__name__}."):
        parts = mod.name.split(".")[2:]
        if any(p.startswith("_") for p in parts) or parts == ["base"]:
            continue
        importlib.import_module(mod.name)
    _loaded = True


def strategy_class(name: str) -> type[Strategy]:
    _autoload()
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"Unknown strategy '{name}'. Available: {', '.join(sorted(_REGISTRY))}"
        ) from None


def get_strategy(name: str, **params: Any) -> Strategy:
    return strategy_class(name)(**params)


def list_strategies() -> list[type[Strategy]]:
    _autoload()
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]
