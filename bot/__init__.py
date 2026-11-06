"""Trading bot infrastructure for the Optibook exchange.

Strategies live in the top-level strategies/ folder (start from strategies/_template.py).
Run them with `python -m bot -s <name>[,<name>...]`, or from a notebook with bot.run([...]).
"""
from bot.engine import Engine, run
from bot.models import ASK, BID, Book, Level, Order
from bot.settings import Settings
from bot.strategy import Context, Strategy

__all__ = ["ASK", "BID", "Book", "Context", "Engine", "Level", "Order", "Settings", "Strategy", "run"]
