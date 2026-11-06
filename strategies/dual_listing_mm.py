"""Dual-listing market maker: quote the illiquid listing around the liquid one's price, hedge every fill.

Both listings are the same company, so they are worth the same. The liquid listing's mid is the
fair value. We rest a bid and an ask on the illiquid listing a little either side of it (inside
its wide spread, so takers trade with us first) and, whenever one fills, trade the opposite way
on the liquid listing straight away. Each round trip earns roughly EDGE minus half the liquid
spread, without betting on where the price goes. Two safety rails: quotes never come closer than
PILLOW to the liquid listing's best prices (so every hedge locks in a profit), and the long-B /
short-A pile that hedging builds up is closed whenever that can be done at no loss.

Run:  python -m bot -s dual_listing_mm --mock --duration 60
With news trading:  python -m bot -s news_trader,dual_listing_mm --mock --duration 60
"""
import time

from bot import Context, Strategy


class DualListingMM(Strategy):
    LIQUID = "PHILIPS_A"     # tight, deep book: our fair value and our hedge venue
    ILLIQUID = "PHILIPS_B"   # wide, thin book: where we quote
    EDGE = 0.20              # quote this far either side of fair value
    VOLUME = 5               # lots per quote
    SKEW = 0.01              # move both quotes down by this much per lot we are long on ILLIQUID
    PILLOW = 0.10            # quote at least this far outside LIQUID's best prices: min profit per lot
    NEWS_PAUSE = 3.0         # seconds without quotes after any social media post

    def on_start(self, ctx: Context) -> None:
        self.paused_until = 0.0

    def on_tick(self, ctx: Context) -> None:
        liquid = ctx.market.book(self.LIQUID)
        if liquid is None or not liquid.is_two_sided:
            ctx.cancel_all(self.ILLIQUID)  # no reliable fair value: don't leave quotes out
            return

        # 1. Fair value: what the company is worth right now, from the market that knows best.
        fair = liquid.mid

        # 2. Hedge: a fill on ILLIQUID leaves us exposed to the price; undo that on LIQUID now.
        #    Own positions only, so trades of other strategies (e.g. news_trader) are left alone.
        net = ctx.own_position(self.LIQUID) + ctx.own_position(self.ILLIQUID)
        if net > 0:
            ctx.sell(self.LIQUID, liquid.best_bid, net, ioc=True)
        elif net < 0:
            ctx.buy(self.LIQUID, liquid.best_ask, -net, ioc=True)

        # 3. Unwind for free: hedged fills pile up as e.g. long B / short A, using position limit.
        #    When B's best bid reaches A's best ask (or the reverse), close both legs at no loss.
        position_b = ctx.own_position(self.ILLIQUID)
        illiquid = ctx.market.book(self.ILLIQUID)
        if net == 0 and illiquid is not None and illiquid.is_two_sided:
            if position_b > 0 and illiquid.best_bid >= liquid.best_ask:
                volume = min(position_b, illiquid.bids[0].volume, liquid.asks[0].volume)
                ctx.sell(self.ILLIQUID, illiquid.best_bid, volume, ioc=True)
                ctx.buy(self.LIQUID, liquid.best_ask, volume, ioc=True)
                ctx.log.info("unwind %d: sell B @ %.2f, buy A @ %.2f", volume, illiquid.best_bid, liquid.best_ask)
            elif position_b < 0 and illiquid.best_ask <= liquid.best_bid:
                volume = min(-position_b, illiquid.asks[0].volume, liquid.bids[0].volume)
                ctx.buy(self.ILLIQUID, illiquid.best_ask, volume, ioc=True)
                ctx.sell(self.LIQUID, liquid.best_bid, volume, ioc=True)
                ctx.log.info("unwind %d: buy B @ %.2f, sell A @ %.2f", volume, illiquid.best_ask, liquid.best_bid)

        # 4. Skew: leaning the quotes against what we hold makes the side that reduces it fill more.
        center = fair - self.SKEW * position_b

        # 5. News guard: right after a post the price can jump, and resting quotes would be traded
        #    at the old price. Keep hedging, but pull the quotes for NEWS_PAUSE seconds.
        now = time.monotonic()
        if ctx.market.new_posts:
            self.paused_until = now + self.NEWS_PAUSE
        if now < self.paused_until:
            ctx.cancel_all(self.ILLIQUID)
            return

        # 6. Quote around fair, but at least PILLOW outside A's best prices, so every fill can be
        #    hedged in A for at least PILLOW per lot. Risk checks clip the volume near the limit.
        bid = min(center - self.EDGE, liquid.best_bid - self.PILLOW)
        ask = max(center + self.EDGE, liquid.best_ask + self.PILLOW)
        ctx.quote(self.ILLIQUID, bid=bid, ask=ask, volume=self.VOLUME)

        # for fill in ctx.fills:
        #     ctx.log.info("%s %s %d @ %.2f", fill.instrument_id, fill.side, fill.volume, fill.price)
