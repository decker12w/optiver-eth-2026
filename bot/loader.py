"""Finds strategies in the top-level `strategies/` folder.

Rules:
  * one file = one strategy, and the file name is the strategy's name
  * the file defines exactly one Strategy subclass
  * files starting with "_" are ignored (templates, shared helpers)

Only the strategies you ask for are imported, so a half-written file elsewhere in the folder
cannot stop the bot from starting.
"""
from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

from bot.strategy import Strategy

PACKAGE = "strategies"
RESERVED_NAMES = {"engine", "unknown"}  # owner names the bot uses itself
_REPO_ROOT = Path(__file__).resolve().parent.parent


class StrategyLoadError(Exception):
    pass


def available() -> list[str]:
    """Names of all strategies in the folder (without importing them)."""
    package = _import(PACKAGE)
    return sorted(m.name for m in pkgutil.iter_modules(package.__path__)
                  if not m.ispkg and not m.name.startswith("_"))


def load(name: str) -> Strategy:
    """Import strategies/<name>.py and return an instance of the Strategy subclass it defines."""
    names = available()
    if name not in names:
        raise StrategyLoadError(f"no strategy named {name!r}; available: {', '.join(names) or 'none'}")
    if name in RESERVED_NAMES:
        raise StrategyLoadError(f"{name!r} is reserved, rename strategies/{name}.py")
    module = _import(f"{PACKAGE}.{name}")
    classes = [obj for obj in vars(module).values()
               if isinstance(obj, type) and issubclass(obj, Strategy) and obj is not Strategy
               and obj.__module__ == module.__name__]
    if len(classes) != 1:
        found = ", ".join(c.__name__ for c in classes) or "none"
        raise StrategyLoadError(f"strategies/{name}.py must define exactly one Strategy subclass (found: {found})")
    strategy = classes[0]()
    strategy.name = name
    return strategy


def load_many(names: list[str]) -> list[Strategy]:
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise StrategyLoadError(f"strategy listed more than once: {', '.join(duplicates)}")
    return [load(name) for name in names]


def describe(name: str) -> str:
    """First line of the strategy's docstring, or the load error, for `--list`."""
    try:
        strategy = load(name)
    except Exception as exc:
        return f"(fails to load: {exc!r})"
    doc = type(strategy).__doc__ or sys.modules[type(strategy).__module__].__doc__ or ""
    return doc.strip().splitlines()[0] if doc.strip() else ""


def _import(module_name: str):
    if str(_REPO_ROOT) not in sys.path:  # works no matter which directory the bot is started from
        sys.path.insert(0, str(_REPO_ROOT))
    return importlib.import_module(module_name)
