"""Single-stock market maker: quote every stock on both sides, lean against inventory, step aside on news.

For each stock: take a fair price (the microprice), then rest a bid one tick above the best bid
and an ask one tick below the best ask (first in line for the next trade), but never closer than
MIN_EDGE_TICKS to fair. Both quotes shift against what we hold, so inventory drifts back toward
zero; at +MAX_POSITION we stop buying, at -MAX_POSITION we stop selling. After a post about a
stock (or about the whole sector, or about nothing we can place) its quotes are pulled for
NEWS_PAUSE seconds, because that is when quotes get traded at stale prices.

There is no second listing to hedge in, so the skew is what keeps the risk small.

Run:  python -m bot -s stock_mm      With news trading:  python -m bot -s news_trader,stock_mm
"""
from __future__ import annotations

import time
from collections import deque

from bot import ASK, BID, Context, Strategy
from strategies._news import about, companies


class StockMM(Strategy):
    STOCKS: list[str] = []   # stocks to quote; empty = every tradable stock
    VOLUME = 5               # lots per quote
    MIN_EDGE_TICKS = 1       # never quote closer than this to fair
    MAX_SKEW_TICKS = 4       # how far quotes lean at MAX_POSITION
    MAX_POSITION = 30        # stop adding beyond this many lots (keep it under the exchange limit)
    REQUOTE_TICKS = 1        # leave a quote alone while it trails its target by at most this much
    NEWS_PAUSE = 3.0         # seconds without quotes after a post about the stock
    MAX_MESSAGES = 12        # own messages per second; the rest of the exchange's limit stays free
                             # for time-critical strategies (news_trader, etf_arb). CONFIRM the limit.

    def on_start(self, ctx: Context) -> None:
        self.paused_until: dict[str, float] = {}
        self.sent: deque[float] = deque()  # times of our recent messages
        self.turn = 0                      # which stock goes first this tick (rotates)

    def on_tick(self, ctx: Context) -> None:
        now = time.monotonic()
        known = companies(ctx.market.instruments)
        stocks = self.STOCKS or sorted(i for ids in known.values() for i in ids if ctx.market.is_tradable(i))
        for post in ctx.market.new_posts:
            named, _ = about(post.post, known)
            hit = [i for key in named for i in known[key]] if named else stocks  # sector/unclear: all
            for instrument_id in hit:
                self.paused_until[instrument_id] = now + self.NEWS_PAUSE

        # Rotate the order so that when the budget runs out, a different stock waits each time.
        self.turn = (self.turn + 1) % max(1, len(stocks))
        while self.sent and now - self.sent[0] > 1.0:
            self.sent.popleft()
        for instrument_id in stocks[self.turn:] + stocks[:self.turn]:
            if len(self.sent) + 4 > self.MAX_MESSAGES:  # a requote can take 2 cancels + 2 inserts
                break
            before = {o.order_id for o in ctx.orders(instrument_id)}
            self._quote(ctx, instrument_id, now)
            after = {o.order_id for o in ctx.orders(instrument_id)}
            self.sent.extend([now] * len(before ^ after))  # each cancelled or new order = 1 message

    def _quote(self, ctx: Context, instrument_id: str, now: float) -> None:
        book = ctx.market.book(instrument_id)  # excludes our own quotes
        if book is None or not book.is_two_sided or now < self.paused_until.get(instrument_id, 0.0):
            ctx.cancel_all(instrument_id)
            return
        tick = ctx.market.tick_size(instrument_id)
        position = ctx.own_position(instrument_id)
        fair = book.microprice
        edge = self.MIN_EDGE_TICKS * tick
        lean = self.MAX_SKEW_TICKS * tick * position / self.MAX_POSITION  # > 0 when long: lower both

        # Improve the best price by a tick, but keep EDGE from fair; lean; never cross the spread.
        bid = min(book.best_bid + tick, fair - edge) - lean
        ask = max(book.best_ask - tick, fair + edge) - lean
        bid = min(bid, book.best_ask - tick)
        ask = max(ask, book.best_bid + tick)

        bid = None if position >= self.MAX_POSITION else self._keep(ctx, instrument_id, BID, bid, tick)
        ask = None if position <= -self.MAX_POSITION else self._keep(ctx, instrument_id, ASK, ask, tick)
        ctx.quote(instrument_id, bid=bid, ask=ask, volume=self.VOLUME)

    def _keep(self, ctx: Context, instrument_id: str, side: str, target: float, tick: float) -> float:
        """Price to quote on `side`: the resting order's price if it is no more aggressive than the
        target and trails it by at most REQUOTE_TICKS (saves cancel/insert messages), else the target."""
        target = ctx.round_price(instrument_id, target, side)
        for order in ctx.orders(instrument_id):
            if order.side == side:
                behind = target - order.price if side == BID else order.price - target
                if -1e-9 <= behind <= self.REQUOTE_TICKS * tick + 1e-9:
                    return order.price
        return target
