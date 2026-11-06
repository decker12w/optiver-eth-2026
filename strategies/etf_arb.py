"""ETF arbitrage: buy the ETF when it is cheaper than its stocks (selling the stocks), or the reverse.

An ETF is a basket: one unit is worth BASKET[stock] shares of each stock plus CASH. When the ETF
can be bought for less than the stocks can be sold for (or sold for more than they cost), we do
both at once with IOC orders and hold the pair. Stock price moves then cancel out between the two
sides, so the gap we traded at is locked in. When the gap flips the other way, we close the pair
at no loss, which frees up position limit for the next trade.

Same idea as dual-listing arbitrage with a basket on one side. Based on the "basket trader" a
HackaTUM 2024 team ran on Optibook (github.com/tobiasleibrock/optibook-algorithmic-trader).

CONFIRM at the event: the ETF's id and exactly what one unit holds (see the instrument
definitions the bot prints at start-up: index_constituents, etf_multiplier, etf_cash_comp).

Run:  python -m bot -s etf_arb    (the repo's --mock has no ETF; the strategy then just waits)
"""
import math
from fractions import Fraction

from bot import ASK, BID, Context, Strategy


class EtfArb(Strategy):
    ETF = "ETF"                                                      # CONFIRM: the ETF's id
    BASKET = {"STOCK_1": 1 / 3, "STOCK_2": 1 / 3, "STOCK_3": 1 / 3}  # CONFIRM: shares per 1 ETF
    CASH = 0.0           # CONFIRM: cash per 1 ETF on top of the shares
    EDGE = 0.10          # only open a trade if it earns at least this much per ETF unit
    MAX_VOLUME = 30      # largest ETF volume per trade

    def on_start(self, ctx: Context) -> None:
        # Smallest ETF volume that matches whole shares of every stock (3 when each weight is 1/3).
        self.lot = math.lcm(*(Fraction(w).limit_denominator(1000).denominator for w in self.BASKET.values()))
        missing = [i for i in (self.ETF, *self.BASKET) if i not in ctx.market.instruments]
        self.ready = not missing
        if missing:
            ctx.log.warning("not trading: unknown instruments %s (set ETF and BASKET)", missing)
        else:
            ctx.log.info("1 %s = %s + %.2f cash; trading in lots of %d", self.ETF, self.BASKET, self.CASH, self.lot)

    def on_tick(self, ctx: Context) -> None:
        if not self.ready:
            return
        books = {i: ctx.market.book(i) for i in (self.ETF, *self.BASKET)}
        if any(b is None or not b.is_two_sided for b in books.values()):
            return

        # 1. Keep the hedge exact: hold -BASKET[stock] shares per ETF unit we own. This also repairs
        #    a leg that did not fill completely last tick. Trade again once the hedge is clean.
        etf_position = ctx.own_position(self.ETF)
        repaired = False
        for stock, weight in self.BASKET.items():
            diff = round(-weight * etf_position) - ctx.own_position(stock)
            if diff > 0:
                repaired = ctx.buy(stock, books[stock].best_ask, diff, ioc=True) is not None or repaired
            elif diff < 0:
                repaired = ctx.sell(stock, books[stock].best_bid, -diff, ioc=True) is not None or repaired
        if repaired:
            return

        # 2. What the basket is worth to us right now: selling all its stocks, or buying them.
        basket_bid = self.CASH + sum(w * books[s].best_bid for s, w in self.BASKET.items())
        basket_ask = self.CASH + sum(w * books[s].best_ask for s, w in self.BASKET.items())
        etf = books[self.ETF]
        cheap = basket_bid - etf.best_ask  # profit per unit: buy the ETF, sell the stocks
        rich = etf.best_bid - basket_ask   # profit per unit: sell the ETF, buy the stocks

        # 3. Open when the gap pays EDGE; close an existing pair as soon as that costs nothing.
        #    (1e-9: sums of float prices land a hair under round numbers, e.g. 0.0999999.)
        edge, free = self.EDGE - 1e-9, -1e-9
        if cheap >= edge or (etf_position < 0 and cheap >= free):
            self._trade(ctx, books, +1, cheap, None if cheap >= edge else -etf_position)
        elif rich >= edge or (etf_position > 0 and rich >= free):
            self._trade(ctx, books, -1, rich, None if rich >= edge else etf_position)

    def _trade(self, ctx: Context, books: dict, sign: int, gap: float, limit: int | None) -> None:
        """Buy (sign +1) or sell (-1) the ETF and do the opposite in every stock, sized so that
        each leg fits in the best price level and under the position limit."""
        etf = books[self.ETF]
        etf_side, stock_side = (BID, ASK) if sign > 0 else (ASK, BID)
        volume = min(self.MAX_VOLUME, (etf.asks if sign > 0 else etf.bids)[0].volume,
                     ctx.room(self.ETF, etf_side))
        if limit is not None:
            volume = min(volume, limit)
        for stock, weight in self.BASKET.items():
            top = (books[stock].bids if sign > 0 else books[stock].asks)[0].volume
            volume = min(volume, int(min(top, ctx.room(stock, stock_side)) / weight + 1e-9))
        volume -= volume % self.lot
        if volume <= 0:
            return

        if sign > 0:
            ctx.buy(self.ETF, etf.best_ask, volume, ioc=True)
            for stock, weight in self.BASKET.items():
                ctx.sell(stock, books[stock].best_bid, round(weight * volume), ioc=True)
        else:
            ctx.sell(self.ETF, etf.best_bid, volume, ioc=True)
            for stock, weight in self.BASKET.items():
                ctx.buy(stock, books[stock].best_ask, round(weight * volume), ioc=True)
        action = "open" if limit is None else "close"
        ctx.log.info("%s: %s %d %s vs basket, gap %+.2f per unit", action,
                     "buy" if sign > 0 else "sell", volume, self.ETF, gap)
