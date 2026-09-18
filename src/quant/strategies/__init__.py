from __future__ import annotations

import importlib
import pkgutil

from quant.strategies.base import (EmptyParams, Strategy, get_strategy,
                                   list_strategies, load_strategy_config,
                                   register_strategy)


def _discover() -> None:
    for module in pkgutil.iter_modules(__path__):
        if module.name not in ("base",) and not module.name.startswith("_"):
            importlib.import_module(f"{__name__}.{module.name}")


_discover()
