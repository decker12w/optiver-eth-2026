"""Market data ingestion: public information about the exchange, refreshed once per tick.

This is the only place that polls public trades and social media posts. Those `poll_*` calls
return each item once, so a second caller would silently steal data from the first.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Iterable

from bot.models import Book, Order


class MarketData:
    """Read-only market view for strategies (`ctx.market`), refreshed by the engine each tick.

    instruments   all instruments by id (optibook Instrument objects, incl. expired/paused)
    tradable      ids that can be traded right now
    book(id)      order book *without our own orders*: what is actually available to trade against
    raw_book(id)  order book exactly as the exchange shows it (our orders included)
    trades(id)    recent public trades, oldest first; new_trades(id) = only those since last tick
    posts         recent social media posts, oldest first; new_posts = only those since last tick
    """

    def __init__(self, exchange, trade_history: int = 1000, post_history: int = 500):
        self._exchange = exchange
        self._trade_history = trade_history
        self.instruments: dict = {}
        self.tradable: frozenset[str] = frozenset()
        self.updated_at: float = 0.0  # time.time() of the last refresh
        self.posts: deque = deque(maxlen=post_history)
        self.new_posts: list = []
        self._raw_books: dict[str, Book] = {}
        self._books: dict[str, Book] = {}
        self._own_orders: dict[str, list[Order]] = {}
        self._trades: dict[str, deque] = {}
        self._new_trades: dict[str, list] = {}
        self._polled_trades: dict[str, list] = {}  # polled but not yet published (see refresh)
        self._polled_posts: list = []

    def refresh(self, own_orders: Iterable[Order] = ()) -> None:
        """Pull the latest state from the exchange client. `own_orders` are hidden from book().

        All or nothing: if a call fails half way (e.g. a disconnect), strategies keep seeing the
        previous snapshot, and trades/posts already polled are kept for the next refresh.
        """
        e = self._exchange
        instruments = dict(e.get_instruments())
        tradable = frozenset(e.get_tradable_instruments())
        raw_books = {}
        for instrument_id in instruments:
            book = self._read_book(instrument_id)
            if book is not None:
                raw_books[instrument_id] = book
            self._polled_trades.setdefault(instrument_id, []).extend(e.poll_new_trade_ticks(instrument_id))
        self._polled_posts.extend(e.poll_new_social_media_feeds())

        # Everything was read: publish the new snapshot.
        self.instruments, self.tradable, self._raw_books = instruments, tradable, raw_books
        self._new_trades, self._polled_trades = self._polled_trades, {}
        for instrument_id, new in self._new_trades.items():
            self._trades.setdefault(instrument_id, deque(maxlen=self._trade_history)).extend(new)
        self.new_posts, self._polled_posts = self._polled_posts, []
        self.posts.extend(self.new_posts)
        self._own_orders = {}
        for order in own_orders:
            self._own_orders.setdefault(order.instrument_id, []).append(order)
        self._books = {}
        self.updated_at = time.time()

    def _read_book(self, instrument_id: str) -> Book | None:
        try:
            price_book = self._exchange.get_last_price_book(instrument_id)
        except AssertionError:  # not connected: fail the whole refresh
            raise
        except Exception:  # one instrument without a book must not stall every other one
            return None
        return Book.from_price_book(price_book) if price_book is not None else None

    # --- books ---

    def book(self, instrument_id: str) -> Book | None:
        """Order book excluding our own resting orders (None if the exchange sent no book yet)."""
        if instrument_id not in self._books:
            raw = self._raw_books.get(instrument_id)
            if raw is None:
                return None
            self._books[instrument_id] = raw.without(self._own_orders.get(instrument_id, ()))
        return self._books[instrument_id]

    def raw_book(self, instrument_id: str) -> Book | None:
        return self._raw_books.get(instrument_id)

    def mid(self, instrument_id: str) -> float | None:
        book = self.book(instrument_id)
        return book.mid if book else None

    def mark(self, instrument_id: str) -> float | None:
        """Best guess of current value for PnL: mid excluding our orders, else last public trade."""
        mid = self.mid(instrument_id)
        if mid is not None:
            return mid
        last = self.last_trade(instrument_id)
        return last.price if last else None

    # --- trades ---

    def trades(self, instrument_id: str) -> deque:
        return self._trades.get(instrument_id, deque())

    def new_trades(self, instrument_id: str) -> list:
        return self._new_trades.get(instrument_id, [])

    def last_trade(self, instrument_id: str):
        trades = self._trades.get(instrument_id)
        return trades[-1] if trades else None

    # --- instruments ---

    def is_tradable(self, instrument_id: str) -> bool:
        return instrument_id in self.tradable

    def tick_size(self, instrument_id: str) -> float:
        return self.instruments[instrument_id].tick_size

    def by_type(self, type_name: str) -> list:
        """Instruments of one type, e.g. by_type("STOCK_OPTION"). Names: STOCK, STOCK_OPTION,
        STOCK_FUTURE, INDEX_TRACKING_ETF, INDEX_OPTION, INDEX_FUTURE."""
        return [instrument for instrument in self.instruments.values()
                if getattr(instrument.instrument_type, "name", instrument.instrument_type) == type_name]
