"""Our private state on the exchange: positions, cash, resting orders and fills.

The exchange client is the source of truth. Every tick `sync()` rebuilds the order view from
`get_outstanding_orders`, so it cannot drift. The bot adds only one thing of its own: which
strategy owns each order, so that fills can be credited to the strategy that earned them.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import replace
from typing import Callable, Optional

from bot.models import IOC, Order, side_sign

UNKNOWN_OWNER = "unknown"  # orders this run did not place, e.g. left over from an earlier run
OWNER_MEMORY_SECONDS = 60.0  # keep an order's owner this long after it was last seen live

Mark = Callable[[str], Optional[float]]  # instrument_id -> valuation price (None if unknown)


class Ledger:
    """Position and cash built from one strategy's own fills (PnL attribution)."""

    def __init__(self) -> None:
        self.positions: dict[str, int] = defaultdict(int)
        self.cash = 0.0
        self.fills = 0
        self.volume = 0

    def apply(self, trade) -> None:
        sign = side_sign(trade.side)
        self.positions[trade.instrument_id] += sign * trade.volume
        self.cash -= sign * trade.volume * trade.price
        self.fills += 1
        self.volume += trade.volume

    def pnl(self, mark: Mark) -> float | None:
        return _pnl(self.cash, self.positions, mark)


class Account:
    """Positions, cash, resting orders (with owners) and per-strategy ledgers."""

    def __init__(self, exchange, clock: Callable[[], float] = time.monotonic):
        self._exchange = exchange
        self._clock = clock
        self.positions: dict[str, int] = {}
        self.cash = 0.0
        self._orders: dict[int, Order] = {}
        self._owners: dict[int, list] = {}  # order_id -> [owner, last time seen live]
        self._ioc_sent: dict[tuple[str, str], int] = defaultdict(int)
        self._unreported: list[tuple[str, object]] = []  # fills polled but not yet returned
        self.ledgers: dict[str, Ledger] = defaultdict(Ledger)

    # --- refresh (engine only) ---

    def sync(self) -> None:
        """Reload positions, cash and resting orders from the exchange client (all or nothing)."""
        e, now = self._exchange, self._clock()
        positions = dict(e.get_positions())
        cash = e.get_cash()
        orders = {}
        for instrument_id in e.get_instruments():
            for order_id, status in e.get_outstanding_orders(instrument_id).items():
                orders[order_id] = Order(order_id, instrument_id, status.side, status.price,
                                         status.volume, self._touch_owner(order_id, now))
        self.positions, self.cash, self._orders = positions, cash, orders
        self._ioc_sent.clear()  # IOCs sent last tick are now reflected in positions

    def poll_fills(self) -> list[tuple[str, object]]:
        """New private trades as (owner, optibook Trade) pairs, credited to each owner's ledger.

        If polling fails half way, the fills already polled are returned by the next call.
        """
        e, now = self._exchange, self._clock()
        for instrument_id in e.get_instruments():
            for trade in e.poll_new_trades(instrument_id):
                entry = self._owners.get(trade.order_id)
                owner = entry[0] if entry else UNKNOWN_OWNER
                self.ledgers[owner].apply(trade)
                self._unreported.append((owner, trade))
        fills, self._unreported = self._unreported, []
        self._owners = {oid: entry for oid, entry in self._owners.items()
                        if oid in self._orders or now - entry[1] < OWNER_MEMORY_SECONDS}
        return fills

    # --- bookkeeping after our own requests (gateway only) ---

    def record_insert(self, order_id: int, owner: str, instrument_id: str, side: str,
                      price: float, volume: int, order_type: str) -> None:
        self._owners[order_id] = [owner, self._clock()]
        if order_type == IOC:
            self._ioc_sent[(instrument_id, side)] += volume
        else:
            self._orders[order_id] = Order(order_id, instrument_id, side, price, volume, owner)

    def record_amend(self, order_id: int, volume: int) -> None:
        if order_id in self._orders:
            self._orders[order_id] = replace(self._orders[order_id], volume=volume)

    def record_delete(self, order_id: int) -> None:
        self._orders.pop(order_id, None)

    def record_delete_all(self, instrument_id: str) -> None:
        for order_id in [o.order_id for o in self._orders.values() if o.instrument_id == instrument_id]:
            del self._orders[order_id]

    # --- queries ---

    def position(self, instrument_id: str) -> int:
        return self.positions.get(instrument_id, 0)

    def order(self, order_id: int) -> Order | None:
        return self._orders.get(order_id)

    def orders(self, instrument_id: str | None = None, owner: str | None = None) -> list[Order]:
        """Resting orders, optionally filtered by instrument and/or owning strategy."""
        return [o for o in self._orders.values()
                if (instrument_id is None or o.instrument_id == instrument_id)
                and (owner is None or o.owner == owner)]

    def resting_volume(self, instrument_id: str, side: str) -> int:
        return sum(o.volume for o in self._orders.values()
                   if o.instrument_id == instrument_id and o.side == side)

    def ioc_volume_this_tick(self, instrument_id: str, side: str) -> int:
        """IOC volume sent since the last sync. Its fills may not be in `positions` yet."""
        return self._ioc_sent.get((instrument_id, side), 0)

    def pnl(self, mark: Mark) -> float | None:
        """Account PnL: cash plus positions valued by `mark`. None if a position has no mark."""
        return _pnl(self.cash, self.positions, mark)

    def _touch_owner(self, order_id: int, now: float) -> str:
        entry = self._owners.get(order_id)
        if entry is None:
            return UNKNOWN_OWNER
        entry[1] = now
        return entry[0]


def _pnl(cash: float, positions: dict[str, int], mark: Mark) -> float | None:
    total = cash
    for instrument_id, position in positions.items():
        if position:
            price = mark(instrument_id)
            if price is None:
                return None
            total += position * price
    return total
