"""The single path from strategies to the exchange.

Every insert goes: validate -> round to tick -> risk check -> throttle -> send -> record.

Error policy:
  * Programming mistakes (bad side, unknown instrument, NaN price) raise ValueError immediately.
  * Anything the exchange or network does (rejection, disconnect, timeout) is logged and
    reported as None/False, never raised, so a network blip cannot crash a strategy.
"""
from __future__ import annotations

import logging
import math
import numbers
import time
from collections import Counter, deque
from typing import Callable

from bot.account import Account
from bot.market import MarketData
from bot.models import LIMIT, ORDER_TYPES, SIDES, round_to_tick
from bot.reporting import LogLimiter
from bot.risk import RiskManager

log = logging.getLogger("bot.gateway")


class Throttle:
    """Caps outgoing messages at `max_per_second` over a sliding one-second window.

    When the budget is spent it sleeps until a slot frees up, so callers never have to handle
    a "too fast" error, and the exchange never sees one.
    """

    def __init__(self, max_per_second: int, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._limit = max(1, int(max_per_second))
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[float] = deque()
        self.waited = 0.0  # total seconds spent sleeping, so slow ticks can be explained

    def acquire(self) -> None:
        self._expire(self._clock())
        while len(self._sent) >= self._limit:
            delay = max(1e-3, 1.0 - (self._clock() - self._sent[0]))
            self._sleep(delay)
            self.waited += delay
            self._expire(self._clock())
        self._sent.append(self._clock())

    def sent_last_second(self) -> int:
        self._expire(self._clock())
        return len(self._sent)

    def _expire(self, now: float) -> None:
        while self._sent and now - self._sent[0] >= 1.0:
            self._sent.popleft()


class OrderGateway:
    def __init__(self, exchange, market: MarketData, account: Account, risk: RiskManager,
                 throttle: Throttle):
        self._exchange = exchange
        self._market = market
        self._account = account
        self._risk = risk
        self._throttle = throttle
        self._limiter = LogLimiter(5.0)
        self.stats: Counter = Counter()     # messages, rejected (by exchange), errors (no response)
        self.refusals: Counter = Counter()  # orders the bot itself declined to send, by reason

    def insert(self, owner: str, instrument_id: str, side: str, price: float, volume: float,
               order_type: str = LIMIT) -> int | None:
        """Send an order. Returns its order_id, or None if nothing was sent or it was rejected.

        The volume is clipped to what risk allows; check `account.order(order_id).volume` (limit
        orders) if the exact size matters.
        """
        self._validate(instrument_id, side, order_type)
        price, volume = float(price), _as_volume(volume)
        if not math.isfinite(price) or price <= 0:
            raise ValueError(f"invalid price {price!r} for {instrument_id}")
        if volume <= 0:
            return None
        if not self._market.is_tradable(instrument_id):
            return self._refuse(owner, instrument_id, "not tradable")
        price = round_to_tick(price, self._market.tick_size(instrument_id), side)
        allowed, reason = self._risk.check(instrument_id, side, price, volume, order_type)
        if allowed <= 0:
            return self._refuse(owner, instrument_id, reason)

        response = self._send(lambda: self._exchange.insert_order(
            instrument_id, price=price, volume=allowed, side=side, order_type=order_type))
        if response is None:
            return None
        if not response.success:
            self._rejected(owner, f"insert {side} {allowed} @ {price} on {instrument_id}", response.error_reason)
            return None
        self._account.record_insert(response.order_id, owner, instrument_id, side, price, allowed, order_type)
        log.debug("%s: inserted %s %s %d @ %s (%s) id=%s", owner, instrument_id, side, allowed,
                  price, order_type, response.order_id)
        return response.order_id

    def amend(self, owner: str, order_id: int, volume: float) -> bool:
        """Change the volume of one of `owner`'s resting orders (the price cannot be amended)."""
        order = self._owned_order(owner, order_id)
        if order is None:
            return False
        volume = _as_volume(volume)
        if volume <= 0:
            return self.delete(owner, order_id)
        if volume > order.volume and volume - order.volume > self._risk.room(order.instrument_id, order.side):
            self._refuse(owner, order.instrument_id, "position limit")
            return False
        response = self._send(lambda: self._exchange.amend_order(
            order.instrument_id, order_id=order_id, volume=volume))
        if response is None:
            return False
        if not response.success:
            self._rejected(owner, f"amend {order_id} to {volume}", response.error_reason)
            return False
        self._account.record_amend(order_id, volume)
        return True

    def delete(self, owner: str, order_id: int) -> bool:
        """Cancel one of `owner`'s resting orders. False if it was already gone or the cancel failed."""
        order = self._owned_order(owner, order_id)
        if order is None:
            return False
        response = self._send(lambda: self._exchange.delete_order(order.instrument_id, order_id=order_id))
        if response is None:
            return False
        # Forget it locally either way: a failed delete almost always means the order already
        # filled, and if it is somehow still live the next sync brings it back.
        self._account.record_delete(order_id)
        if not response.success:
            log.debug("%s: delete %s failed: %s", owner, order_id, response.error_reason)
        return bool(response.success)

    def delete_all(self, owner: str, instrument_id: str | None = None) -> None:
        """Cancel all of `owner`'s resting orders, optionally on one instrument only."""
        for order in self._account.orders(instrument_id, owner):
            self.delete(owner, order.order_id)

    def delete_everything(self) -> None:
        """Cancel every resting order on the account, whoever placed it (engine use only)."""
        for instrument_id in sorted({o.instrument_id for o in self._account.orders()}):
            if self._send(lambda: self._exchange.delete_orders(instrument_id), expect_response=False):
                self._account.record_delete_all(instrument_id)

    def messages_last_second(self) -> int:
        return self._throttle.sent_last_second()

    def throttled_seconds(self) -> float:
        """Total time spent waiting for the message rate limit."""
        return self._throttle.waited

    # --- internals ---

    def _send(self, request: Callable, expect_response: bool = True):
        """Throttle, then run one exchange request. Returns its response, or None if it raised.

        With expect_response=False (requests that return nothing) returns True on success.
        """
        self._throttle.acquire()
        self.stats["messages"] += 1
        try:
            response = request()
        except Exception as exc:  # disconnects, timeouts, client assertions
            self.stats["errors"] += 1
            if self._limiter.allow(("send", type(exc))):
                log.warning("exchange request failed: %r", exc)
            return None
        return response if expect_response else True

    def _validate(self, instrument_id: str, side: str, order_type: str) -> None:
        if side not in SIDES:
            raise ValueError(f"side must be one of {SIDES}, got {side!r}")
        if order_type not in ORDER_TYPES:
            raise ValueError(f"order_type must be one of {ORDER_TYPES}, got {order_type!r}")
        if instrument_id not in self._market.instruments:
            raise ValueError(f"unknown instrument {instrument_id!r}")

    def _owned_order(self, owner: str, order_id: int):
        order = self._account.order(order_id)
        if order is None:
            return None  # already filled or cancelled
        if order.owner != owner:
            raise ValueError(f"{owner} cannot modify order {order_id}, it belongs to {order.owner}")
        return order

    def _refuse(self, owner: str, instrument_id: str, reason: str) -> None:
        self.refusals[reason] += 1
        log.debug("%s: refused order on %s: %s", owner, instrument_id, reason)
        return None

    def _rejected(self, owner: str, what: str, reason: str | None) -> None:
        self.stats["rejected"] += 1
        if self._limiter.allow(("rejected", owner, reason)):
            log.warning("%s: exchange rejected %s: %s", owner, what, reason)


def _as_volume(volume: float) -> int:
    if isinstance(volume, bool) or not isinstance(volume, numbers.Real) or not math.isfinite(volume):
        raise ValueError(f"invalid volume {volume!r}")
    return int(volume)
