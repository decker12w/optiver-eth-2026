"""Offline stand-in for optibook's synchronous Exchange, to run and test the bot without the platform.

It implements the documented Exchange methods with the same names, arguments and return shapes.
The default market is a dual listing: two order books on one company (PHILIPS_A liquid,
PHILIPS_B illiquid) whose prices follow a shared random-walk fair value plus small,
mean-reverting gaps, with the occasional news post that moves the price.

Anything the docs do not specify (fill rules, rejection reasons, position-limit behaviour) is
a guess. Use the mock to check that code works, never to tune a strategy's numbers.
"""
from __future__ import annotations

import math
import random
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import SimpleNamespace
from typing import Callable

BID, ASK = "bid", "ask"
US, MARKET = "mock_user", "market"  # buyer/seller names in public trade ticks


class InstrumentType(Enum):  # same members as optibook.common_types.InstrumentType
    STOCK = 1
    STOCK_OPTION = 2
    STOCK_FUTURE = 3
    INDEX_TRACKING_ETF = 4
    INDEX_OPTION = 5
    INDEX_FUTURE = 6


# Same attribute names as optibook's response and data classes.
@dataclass
class InsertOrderResponse:
    success: bool
    order_id: int | None
    error_reason: str | None


@dataclass
class AmendOrderResponse:
    success: bool
    error_reason: str | None


@dataclass
class DeleteOrderResponse:
    success: bool
    error_reason: str | None


@dataclass
class OrderStatus:
    order_id: int
    instrument_id: str
    price: float
    volume: int
    side: str


@dataclass
class PriceVolume:
    price: float
    volume: int


@dataclass
class PriceBook:
    timestamp: datetime
    instrument_id: str
    bids: list
    asks: list


@dataclass
class Trade:
    timestamp: datetime
    order_id: int
    trade_id: int
    instrument_id: str
    price: float
    volume: int
    side: str


@dataclass
class TradeTick:
    timestamp: datetime
    instrument_id: str
    price: float
    volume: int
    aggressor_side: str
    buyer: str
    seller: str
    trade_id: int


@dataclass
class SocialMediaFeed:
    timestamp: datetime
    post: str
    meta_data: str = ""


class MockInstrument(SimpleNamespace):
    def __repr__(self) -> str:  # like optibook's: only the attributes that are set
        shown = ("instrument_id", "tick_size", "instrument_type", "base_instrument_id", "expiry",
                 "option_kind", "strike", "instrument_group", "paused", "expired")
        return "Instrument(" + ", ".join(f"{k}={getattr(self, k)}" for k in shown
                                         if getattr(self, k) is not None) + ")"


@dataclass(frozen=True)
class MarketSpec:
    spread_ticks: int                # distance between the synthetic best bid and best ask
    levels: int                      # synthetic price levels per side
    level_volume: tuple[int, int]    # random volume range per level
    taker_probability: float         # chance per step that someone trades at the touch
    taker_volume: tuple[int, int]


DUAL_LISTING = {
    "PHILIPS_A": MarketSpec(spread_ticks=1, levels=5, level_volume=(20, 80),
                            taker_probability=0.4, taker_volume=(1, 40)),
    "PHILIPS_B": MarketSpec(spread_ticks=5, levels=3, level_volume=(2, 15),
                            taker_probability=0.15, taker_volume=(1, 10)),
}

NEWS = [
    ("PHILIPS beats earnings expectations", 0.02),
    ("PHILIPS issues profit warning", -0.03),
    ("Regulator opens probe into PHILIPS", -0.015),
    ("PHILIPS wins major hospital contract", 0.015),
    ("Analysts reiterate neutral view on PHILIPS", 0.0),
]


class MockExchange:
    def __init__(self, seed: int | None = None, specs: dict[str, MarketSpec] | None = None,
                 tick_size: float = 0.1, start_price: float = 100.0, position_limit: int = 100,
                 step_seconds: float = 0.05, auto_step: bool = True,
                 disconnect_probability: float = 0.0, news_probability: float = 0.001,
                 clock: Callable[[], float] = time.monotonic):
        """auto_step=True advances the market with wall-clock time; tests use auto_step=False and
        call step() themselves for deterministic runs."""
        self._rng = random.Random(seed)
        self._specs = dict(specs or DUAL_LISTING)
        self._tick = tick_size
        self._fair = start_price
        self._position_limit = position_limit
        self._step_seconds = step_seconds
        self._auto_step = auto_step
        self._disconnect_probability = disconnect_probability
        self._news_probability = news_probability
        self._clock = clock
        self._last_step = clock()
        self._connected = False
        self.requests = 0  # insert/amend/delete calls received, to check message usage

        ids = list(self._specs)
        self._offset = {i: 0.0 for i in ids}
        self._levels: dict[str, dict[str, list[list]]] = {}  # synthetic liquidity, best first
        self._orders: dict[str, dict[int, OrderStatus]] = {i: {} for i in ids}
        self._positions = {i: 0 for i in ids}
        self._cash = {i: 0.0 for i in ids}
        self._new_trades: dict[str, list] = {i: [] for i in ids}
        self._trades = {i: deque(maxlen=100) for i in ids}
        self._new_ticks: dict[str, list] = {i: [] for i in ids}
        self._ticks = {i: deque(maxlen=100) for i in ids}
        self._new_posts: list = []
        self._posts: deque = deque(maxlen=100)
        self._next_id = 1
        for instrument_id in ids:
            self._rebuild_levels(instrument_id)

    # --- connection ---

    def connect(self) -> None:
        self._connected = True
        self._last_step = self._clock()

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.disconnect()

    # --- market data ---

    def get_instruments(self) -> dict:
        self._ready()
        return {i: self._instrument(i) for i in self._specs}

    def get_tradable_instruments(self) -> dict:
        return self.get_instruments()

    def get_last_price_book(self, instrument_id: str) -> PriceBook:
        self._ready()
        sides = {}
        for side, reverse in ((BID, True), (ASK, False)):
            volume_at: dict[float, int] = {}
            for price, volume in self._levels[instrument_id][side]:
                volume_at[price] = volume_at.get(price, 0) + volume
            for order in self._orders[instrument_id].values():
                if order.side == side:
                    volume_at[order.price] = volume_at.get(order.price, 0) + order.volume
            sides[side] = [PriceVolume(p, v) for p, v in sorted(volume_at.items(), reverse=reverse) if v > 0]
        return PriceBook(datetime.now(), instrument_id, sides[BID], sides[ASK])

    def poll_new_trade_ticks(self, instrument_id: str) -> list:
        self._ready()
        new, self._new_ticks[instrument_id] = self._new_ticks[instrument_id], []
        return new

    def get_trade_tick_history(self, instrument_id: str) -> list:
        self._ready()
        return list(self._ticks[instrument_id])

    def poll_new_social_media_feeds(self) -> list:
        self._ready()
        new, self._new_posts = self._new_posts, []
        return new

    def get_social_media_feeds_history(self) -> list:
        self._ready()
        return list(self._posts)

    # --- orders ---

    def insert_order(self, instrument_id: str, *, price: float, volume: int, side: str,
                     order_type: str = "limit") -> InsertOrderResponse:
        self._ready()
        assert side in (BID, ASK), f"Invalid value ({side}) for parameter 'side'"
        assert order_type in ("limit", "ioc"), "order_type must be one of ['limit', 'ioc']"
        self.requests += 1
        error = self._order_error(instrument_id, price, volume, side)
        if error:
            return InsertOrderResponse(False, None, error)
        order_id = self._new_id()
        remaining = self._match(instrument_id, order_id, side, price, volume)
        if remaining and order_type == "limit":
            self._orders[instrument_id][order_id] = OrderStatus(order_id, instrument_id, price, remaining, side)
        return InsertOrderResponse(True, order_id, None)

    def amend_order(self, instrument_id: str, *, order_id: int, volume: int) -> AmendOrderResponse:
        self._ready()
        self.requests += 1
        order = self._orders.get(instrument_id, {}).get(order_id)
        if order is None:
            return AmendOrderResponse(False, "Order not found")
        if not isinstance(volume, int) or volume <= 0:
            return AmendOrderResponse(False, "Invalid volume")
        if self._breaches_limit(instrument_id, order.side, volume - order.volume):
            return AmendOrderResponse(False, "Position limit would be exceeded")
        order.volume = volume
        return AmendOrderResponse(True, None)

    def delete_order(self, instrument_id: str, *, order_id: int) -> DeleteOrderResponse:
        self._ready()
        self.requests += 1
        if self._orders.get(instrument_id, {}).pop(order_id, None) is None:
            return DeleteOrderResponse(False, "Order not found")
        return DeleteOrderResponse(True, None)

    def delete_orders(self, instrument_id: str) -> None:
        self._ready()
        self.requests += 1
        self._orders[instrument_id].clear()

    def get_outstanding_orders(self, instrument_id: str) -> dict:
        self._ready()
        return {oid: OrderStatus(o.order_id, o.instrument_id, o.price, o.volume, o.side)
                for oid, o in self._orders[instrument_id].items()}

    # --- account ---

    def poll_new_trades(self, instrument_id: str) -> list:
        self._ready()
        new, self._new_trades[instrument_id] = self._new_trades[instrument_id], []
        return new

    def get_trade_history(self, instrument_id: str) -> list:
        self._ready()
        return list(self._trades[instrument_id])

    def get_positions(self) -> dict:
        self._ready()
        return dict(self._positions)

    def get_positions_and_cash(self) -> dict:
        self._ready()
        return {i: {"volume": self._positions[i], "cash": self._cash[i]} for i in self._specs}

    def get_cash(self) -> float:
        self._ready()
        return sum(self._cash.values())

    def get_pnl(self, valuations: dict | None = None) -> float | None:
        self._ready()
        total = self.get_cash()
        for instrument_id, position in self._positions.items():
            if position:
                price = (valuations or {}).get(instrument_id)
                if price is None and self._ticks[instrument_id]:
                    price = self._ticks[instrument_id][-1].price
                if price is None:
                    return None
                total += position * price
        return total

    # --- simulation ---

    def step(self, n: int = 1) -> None:
        """Advance the simulated market by `n` steps."""
        for _ in range(n):
            self._step()

    def _ready(self) -> None:
        if not self._connected:  # the real client asserts the same way
            raise AssertionError("Cannot call function until connected. Call connect() first")
        if self._auto_step:
            steps = int((self._clock() - self._last_step) / self._step_seconds)
            if steps:
                self._last_step += steps * self._step_seconds
                self.step(min(steps, 100))

    def _step(self) -> None:
        rng = self._rng
        if rng.random() < self._disconnect_probability:
            self._connected = False
        if rng.random() < self._news_probability:
            post, move = rng.choice(NEWS)
            self._fair *= 1 + move
            self._new_posts.append(SocialMediaFeed(datetime.now(), post))
            self._posts.append(self._new_posts[-1])
        self._fair = max(10 * self._tick, self._fair + rng.gauss(0, 0.02))
        for instrument_id, spec in self._specs.items():
            self._offset[instrument_id] = 0.9 * self._offset[instrument_id] + rng.gauss(0, 0.03)
            self._rebuild_levels(instrument_id)
            self._fill_orders_market_moved_through(instrument_id)
            if rng.random() < spec.taker_probability:
                self._random_taker(instrument_id, spec)

    def _rebuild_levels(self, instrument_id: str) -> None:
        spec, tick = self._specs[instrument_id], self._tick
        center = (self._fair + self._offset[instrument_id]) / tick
        best_bid = round(center - spec.spread_ticks / 2)
        best_ask = best_bid + spec.spread_ticks
        self._levels[instrument_id] = {
            BID: [[round((best_bid - k) * tick, 10), self._rng.randint(*spec.level_volume)] for k in range(spec.levels)],
            ASK: [[round((best_ask + k) * tick, 10), self._rng.randint(*spec.level_volume)] for k in range(spec.levels)],
        }

    def _fill_orders_market_moved_through(self, instrument_id: str) -> None:
        levels = self._levels[instrument_id]
        best_bid, best_ask = levels[BID][0][0], levels[ASK][0][0]
        for order in list(self._orders[instrument_id].values()):
            if (order.side == BID and order.price >= best_ask) or (order.side == ASK and order.price <= best_bid):
                self._fill_resting(order, order.volume)

    def _random_taker(self, instrument_id: str, spec: MarketSpec) -> None:
        """Someone trades at the touch. Our orders priced better than the market fill first; at the
        same price the market's volume is ahead of us in the queue."""
        taker_side = self._rng.choice((BID, ASK))
        resting_side = ASK if taker_side == BID else BID
        remaining = self._rng.randint(*spec.taker_volume)
        level = self._levels[instrument_id][resting_side][0]
        better = (lambda p: p < level[0]) if resting_side == ASK else (lambda p: p > level[0])
        ours = sorted((o for o in self._orders[instrument_id].values() if o.side == resting_side),
                      key=lambda o: (o.price if resting_side == ASK else -o.price, o.order_id))

        for order in [o for o in ours if better(o.price)]:
            remaining -= self._fill_resting(order, min(remaining, order.volume))
            if remaining <= 0:
                return
        market_volume = min(remaining, level[1])
        if market_volume > 0:
            level[1] -= market_volume
            remaining -= market_volume
            self._tick_trade(instrument_id, level[0], market_volume, taker_side, MARKET, MARKET)
        for order in [o for o in ours if o.price == level[0]]:
            if remaining <= 0:
                return
            remaining -= self._fill_resting(order, min(remaining, order.volume))

    def _match(self, instrument_id: str, order_id: int, side: str, price: float, volume: int) -> int:
        """Trade our incoming order against the synthetic book; return the unfilled volume."""
        crosses = (lambda p: p <= price) if side == BID else (lambda p: p >= price)
        for level in self._levels[instrument_id][ASK if side == BID else BID]:
            if volume <= 0 or not crosses(level[0]):
                break
            traded = min(volume, level[1])
            if traded > 0:
                level[1] -= traded
                volume -= traded
                self._our_fill(instrument_id, order_id, side, level[0], traded, aggressor=side)
        return volume

    def _fill_resting(self, order: OrderStatus, volume: int) -> int:
        self._our_fill(order.instrument_id, order.order_id, order.side, order.price, volume,
                       aggressor=ASK if order.side == BID else BID)
        order.volume -= volume
        if order.volume <= 0:
            del self._orders[order.instrument_id][order.order_id]
        return volume

    def _our_fill(self, instrument_id: str, order_id: int, side: str, price: float, volume: int,
                  aggressor: str) -> None:
        sign = 1 if side == BID else -1
        self._positions[instrument_id] += sign * volume
        self._cash[instrument_id] -= sign * volume * price
        trade = Trade(datetime.now(), order_id, self._new_id(), instrument_id, price, volume, side)
        self._new_trades[instrument_id].append(trade)
        self._trades[instrument_id].append(trade)
        buyer, seller = (US, MARKET) if side == BID else (MARKET, US)
        self._tick_trade(instrument_id, price, volume, aggressor, buyer, seller)

    def _tick_trade(self, instrument_id: str, price: float, volume: int, aggressor: str,
                    buyer: str, seller: str) -> None:
        tick = TradeTick(datetime.now(), instrument_id, price, volume, aggressor, buyer, seller, self._new_id())
        self._new_ticks[instrument_id].append(tick)
        self._ticks[instrument_id].append(tick)

    def _order_error(self, instrument_id: str, price: float, volume: int, side: str) -> str | None:
        if instrument_id not in self._specs:
            return f"Unknown instrument {instrument_id}"
        if not isinstance(volume, int) or volume <= 0:
            return "Volume must be a positive integer"
        if not math.isfinite(price) or abs(price / self._tick - round(price / self._tick)) > 1e-6:
            return "Price is not a multiple of the tick size"
        if self._breaches_limit(instrument_id, side, volume):
            return "Position limit would be exceeded"
        return None

    def _breaches_limit(self, instrument_id: str, side: str, extra_volume: int) -> bool:
        """Worst case: current position plus all resting orders on `side` plus `extra_volume`."""
        resting = sum(o.volume for o in self._orders[instrument_id].values() if o.side == side)
        sign = 1 if side == BID else -1
        return abs(self._positions[instrument_id] + sign * (resting + extra_volume)) > self._position_limit

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _instrument(self, instrument_id: str) -> MockInstrument:
        # Every attribute optibook's Instrument has, so strategy code works unchanged.
        return MockInstrument(
            instrument_id=instrument_id, tick_size=self._tick, instrument_type=InstrumentType.STOCK,
            price_change_limit=None, expiry=None, option_kind=None, strike=None,
            base_instrument_id=None, interest_rate=None, index_id=None, index_constituents=None,
            index_divisor=None, index_volatility=None, etf_cash_comp=None, etf_multiplier=None,
            instrument_group="PHILIPS", paused=False, expired=False, parameters=None,
        )
