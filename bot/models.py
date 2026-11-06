"""Plain data types shared across the bot, plus price/tick helpers."""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Iterable

BID = "bid"  # buy
ASK = "ask"  # sell
SIDES = (BID, ASK)

LIMIT = "limit"  # rests in the book until filled or cancelled
IOC = "ioc"      # immediate-or-cancel: whatever does not trade at once is cancelled
ORDER_TYPES = (LIMIT, IOC)

_EPS = 1e-6  # tolerance, in ticks, for float noise when snapping to the tick grid


def side_sign(side: str) -> int:
    """+1 for a buy, -1 for a sell: the position change per lot traded on `side`."""
    return 1 if side == BID else -1


def opposite(side: str) -> str:
    return ASK if side == BID else BID


def round_to_tick(price: float, tick_size: float, side: str) -> float:
    """Snap `price` to the tick grid, never in the aggressive direction: bids round down, asks up."""
    ticks = price / tick_size
    n = math.floor(ticks + _EPS) if side == BID else math.ceil(ticks - _EPS)
    return round(n * tick_size, 10)


def same_price(a: float, b: float) -> bool:
    return abs(a - b) < 1e-9


@dataclass(frozen=True)
class Level:
    price: float
    volume: int


@dataclass(frozen=True)
class Order:
    """One of our resting limit orders, as last seen by the bot."""
    order_id: int
    instrument_id: str
    side: str
    price: float
    volume: int  # remaining (unfilled) volume
    owner: str   # name of the strategy that placed it


@dataclass(frozen=True)
class Book:
    """Immutable order-book snapshot. `bids` are sorted best (highest) first, `asks` best (lowest) first."""
    instrument_id: str
    bids: tuple[Level, ...] = ()
    asks: tuple[Level, ...] = ()
    timestamp: datetime | None = None

    @classmethod
    def from_price_book(cls, price_book) -> Book:
        """Convert optibook's PriceBook (lists of PriceVolume) into a Book."""
        return cls(
            instrument_id=price_book.instrument_id,
            bids=tuple(Level(level.price, level.volume) for level in price_book.bids),
            asks=tuple(Level(level.price, level.volume) for level in price_book.asks),
            timestamp=price_book.timestamp,
        )

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def bid_volume(self) -> int:
        """Volume at the best bid (0 if there are no bids)."""
        return self.bids[0].volume if self.bids else 0

    @property
    def ask_volume(self) -> int:
        """Volume at the best ask (0 if there are no asks)."""
        return self.asks[0].volume if self.asks else 0

    @property
    def is_two_sided(self) -> bool:
        return bool(self.bids) and bool(self.asks)

    @property
    def mid(self) -> float | None:
        if not self.is_two_sided:
            return None
        return (self.bids[0].price + self.asks[0].price) / 2

    @property
    def spread(self) -> float | None:
        if not self.is_two_sided:
            return None
        return self.asks[0].price - self.bids[0].price

    @property
    def microprice(self) -> float | None:
        """Mid weighted by top-of-book volume; leans toward the side with less volume."""
        if not self.is_two_sided:
            return None
        bid, ask = self.bids[0], self.asks[0]
        return (bid.price * ask.volume + ask.price * bid.volume) / (bid.volume + ask.volume)

    def sweep_price(self, side: str, volume: int) -> float | None:
        """Worst price an order on `side` must accept to trade `volume` lots right now.

        A buy walks up the asks, a sell walks down the bids. If the book is too thin, this is the
        last level's price. Returns None if the opposite side is empty.
        """
        levels = self.asks if side == BID else self.bids
        price, remaining = None, volume
        for level in levels:
            price, remaining = level.price, remaining - level.volume
            if remaining <= 0:
                break
        return price

    def without(self, orders: Iterable[Order]) -> Book:
        """This book with the volume of `orders` taken out. Used to hide our own quotes."""
        own: dict[str, dict[float, int]] = {BID: defaultdict(int), ASK: defaultdict(int)}
        for order in orders:
            own[order.side][_price_key(order.price)] += order.volume
        if not own[BID] and not own[ASK]:
            return self
        return replace(self, bids=_subtract(self.bids, own[BID]), asks=_subtract(self.asks, own[ASK]))


def _price_key(price: float) -> float:
    return round(price, 9)


def _subtract(levels: tuple[Level, ...], own: dict[float, int]) -> tuple[Level, ...]:
    out = []
    for level in levels:
        remaining = level.volume - own.get(_price_key(level.price), 0)
        if remaining > 0:
            out.append(level if remaining == level.volume else Level(level.price, remaining))
    return tuple(out)
