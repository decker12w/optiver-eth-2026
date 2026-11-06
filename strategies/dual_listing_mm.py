"""Dual-listing market maker: quote the illiquid listing around the liquid one's price, hedge every fill.

Both listings are the same company, so they are worth the same. The liquid listing's mid is the
fair value. We rest a bid and an ask on the illiquid listing a little either side of it (inside
its wide spread, so takers trade with us first) and, whenever one fills, trade the opposite way
on the liquid listing straight away. Each round trip earns roughly EDGE minus half the liquid
spread, without betting on where the price goes.

Run:  python -m bot -s dual_listing_mm --mock --duration 60
"""
from bot import Context, Strategy


class DualListingMM(Strategy):
    LIQUID = "PHILIPS_A"     # tight, deep book: our fair value and our hedge venue
    ILLIQUID = "PHILIPS_B"   # wide, thin book: where we quote
    EDGE = 0.20              # quote this far either side of fair value
    VOLUME = 5               # lots per quote
    SKEW = 0.01              # move both quotes down by this much per lot we are long on ILLIQUID

    def on_tick(self, ctx: Context) -> None:
        liquid = ctx.market.book(self.LIQUID)
        if liquid is None or not liquid.is_two_sided:
            ctx.cancel_all(self.ILLIQUID)  # no reliable fair value: don't leave quotes out
            return

        # 1. Fair value: what the company is worth right now, from the market that knows best.
        fair = liquid.mid

        # 2. Hedge: a fill on ILLIQUID leaves us exposed to the price; undo that on LIQUID now.
        net = ctx.position(self.LIQUID) + ctx.position(self.ILLIQUID)
        if net > 0:
            ctx.sell(self.LIQUID, liquid.best_bid, net, ioc=True)
        elif net < 0:
            ctx.buy(self.LIQUID, liquid.best_ask, -net, ioc=True)

        # 3. Skew: after hedging we still hold e.g. long B / short A, which uses up position limit.
        #    Leaning the quotes against it makes the side that unwinds it more likely to fill.
        center = fair - self.SKEW * ctx.position(self.ILLIQUID)

        # 4. Quote: one bid and one ask on ILLIQUID (risk checks clip them near the position limit).
        ctx.quote(self.ILLIQUID, bid=center - self.EDGE, ask=center + self.EDGE, volume=self.VOLUME)

        # for fill in ctx.fills:
        #     ctx.log.info("%s %s %d @ %.2f", fill.instrument_id, fill.side, fill.volume, fill.price)
