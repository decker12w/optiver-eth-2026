"""News trader: scores each social media post with word lists and trades big in the news' direction.

How a post is scored (a few microseconds, no model):
  1. Lowercase it and split it into words.
  2. Look up known phrases first ("profit warning", "beats expectations"), then single words.
     Each match adds its weight: positive = good news, negative = bad news. The words inside a
     matched phrase are not counted again ("profit warning" is -2, not "profit" +1 "warning" -1).
  3. A negation just before a word ("not", "denies", ...) cancels it, so "PHILIPS denies fraud"
     is not treated as bad news. Cancelling instead of flipping keeps the bot out of unclear news.
  4. score >= MIN_SCORE: good news, buy.  score <= -MIN_SCORE: bad news, sell.  Otherwise ignore.

Which stock: the company the post names (ticker or name, see ALIASES in strategies/_news.py).
A post naming no company but using sector words ("chip stocks", "AI", "tariffs") trades every
stock at SECTOR_SIZE of the normal size. A post naming several companies is skipped.

How it trades: IOC orders on the company's most liquid stock, VOLUME_PER_POINT lots per point
of score (capped at MAX_VOLUME). It tries to fill for ENTRY_SECONDS, holds until HOLD_SECONDS
after the post (or until the price moves STOP_LOSS against it), then flattens.

Every post is logged with its score and the words that matched, so the word lists (in
strategies/_news.py) can be checked against what the market actually did.

Run on its own:  python -m bot -s news_trader --mock --duration 120
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from bot import Context, Strategy
from strategies._news import about, companies, score  # word lists and company names live there


@dataclass
class Bet:
    sign: int            # +1 long (good news), -1 short (bad news)
    volume: int          # lots we want to hold
    entry_mid: float     # mid when the news arrived, for the stop loss
    entry_until: float   # keep trying to get filled until then
    exit_at: float       # flatten at this time
    exiting: bool = False


class NewsTrader(Strategy):
    MIN_SCORE = 1            # ignore posts scoring less than this either way
    VOLUME_PER_POINT = 40    # lots per point of score: mild news 40, strong news 80
    MAX_VOLUME = 80          # cap per bet (the position limit clips it further)
    MAX_SLIPPAGE_TICKS = 3   # IOCs may trade up to this many ticks beyond the best price
    ENTRY_SECONDS = 1.0      # stop chasing the entry after this long: the move is gone
    HOLD_SECONDS = 5.0       # CALIBRATE at the event: how long prices take to react to a post
    STOP_LOSS = 0.01         # exit early if the price moves 1% against us
    SECTOR_SIZE = 0.5        # sector-wide news: trade every stock, at this fraction of the size

    def on_start(self, ctx: Context) -> None:
        self.bets: dict[str, Bet] = {}
        ctx.log.info("companies: %s", companies(ctx.market.instruments))

    def on_tick(self, ctx: Context) -> None:
        now = time.monotonic()
        for post in ctx.market.new_posts:
            self._on_post(ctx, post.post, now)
        for instrument_id, bet in list(self.bets.items()):
            self._manage(ctx, instrument_id, bet, now)

    def on_stop(self, ctx: Context) -> None:
        for instrument_id in self.bets:  # don't leave a news bet open after the bot stops
            self._trade_towards(ctx, instrument_id, 0)

    # --- deciding ---

    def _on_post(self, ctx: Context, text: str, now: float) -> None:
        value, hits = score(text)
        ctx.log.info("NEWS score %+d %s | %s", value, " ".join(hits) or "-", text)
        if abs(value) < self.MIN_SCORE:
            return
        # One company named: trade it. None named but sector words: trade all, smaller.
        # Several named ("X takes share from Y"): unclear who wins, so stay out.
        known = companies(ctx.market.instruments)
        named, sector = about(text, known)
        if len(named) == 1:
            targets, size = named, 1.0
        elif sector:
            targets, size = set(known), self.SECTOR_SIZE
        else:
            ctx.log.info("not trading: post names %s", sorted(named) or "no known company")
            return
        sign = 1 if value > 0 else -1
        volume = min(self.MAX_VOLUME, round(self.VOLUME_PER_POINT * abs(value) * size))
        for key in sorted(targets):
            instrument_id = self._most_liquid(ctx, known[key])
            if instrument_id is not None:
                self._bet(ctx, instrument_id, sign, volume, now)

    def _bet(self, ctx: Context, instrument_id: str, sign: int, volume: int, now: float) -> None:
        old = self.bets.get(instrument_id)
        if old is not None and old.sign == sign and not old.exiting:
            volume = max(volume, old.volume)  # more news the same way: keep the bigger bet
        self.bets[instrument_id] = Bet(sign, volume, ctx.market.mid(instrument_id),
                                       now + self.ENTRY_SECONDS, now + self.HOLD_SECONDS)
        ctx.log.info("%s %d %s on the news", "BUY" if sign > 0 else "SELL", volume, instrument_id)

    def _most_liquid(self, ctx: Context, instrument_ids: list[str]) -> str | None:
        """The listing with the tightest spread (then the most volume) among one company's stocks."""
        books = [(ctx.market.book(i), i) for i in instrument_ids]
        books = [(b.spread, -(b.bid_volume + b.ask_volume), i) for b, i in books if b and b.is_two_sided]
        return min(books)[2] if books else None

    # --- trading ---

    def _manage(self, ctx: Context, instrument_id: str, bet: Bet, now: float) -> None:
        position = ctx.own_position(instrument_id)
        mid = ctx.market.mid(instrument_id)
        if mid is None:
            return
        against = (bet.entry_mid - mid) * bet.sign  # > 0 when the price moved against us
        if not bet.exiting and (now >= bet.exit_at or against >= self.STOP_LOSS * bet.entry_mid):
            bet.exiting = True
            ctx.log.info("exit %s %+d (%s)", instrument_id, position,
                         "hold time over" if now >= bet.exit_at else "stop loss")
        if bet.exiting:
            target = 0
        elif now <= bet.entry_until:
            target = bet.sign * bet.volume
        else:
            target = position  # entry window over: keep what we got, don't chase the price
        if bet.exiting and position == 0:
            del self.bets[instrument_id]
            return
        self._trade_towards(ctx, instrument_id, target)

    def _trade_towards(self, ctx: Context, instrument_id: str, target: int) -> None:
        book = ctx.market.book(instrument_id)
        diff = target - ctx.own_position(instrument_id)
        if not diff or book is None or not book.is_two_sided:
            return
        slip = self.MAX_SLIPPAGE_TICKS * ctx.market.tick_size(instrument_id)
        if diff > 0:
            ctx.buy(instrument_id, book.best_ask + slip, diff, ioc=True)
        else:
            ctx.sell(instrument_id, book.best_bid - slip, -diff, ioc=True)
