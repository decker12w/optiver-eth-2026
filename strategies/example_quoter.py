"""Demo market maker: quotes both sides of every tradable instrument around a fair value.

Not a real strategy; it exists to exercise the infrastructure. It uses the fair values that
example_signals publishes when that runs first (-s example_signals,example_quoter), and the
book mid otherwise.
"""
from bot import Context, Strategy


class ExampleQuoter(Strategy):
    EDGE_TICKS = 2  # quote this many ticks either side of fair
    VOLUME = 5

    def on_tick(self, ctx: Context) -> None:
        fair_values = ctx.shared.get("fair_value", {})
        for instrument_id in sorted(ctx.market.tradable):
            fair = fair_values.get(instrument_id) or ctx.market.mid(instrument_id)
            if fair is None:
                ctx.cancel_all(instrument_id)
                continue
            edge = self.EDGE_TICKS * ctx.market.tick_size(instrument_id)
            ctx.quote(instrument_id, bid=fair - edge, ask=fair + edge, volume=self.VOLUME)

        for fill in ctx.fills:
            ctx.log.info("%s %s %d @ %.2f (position now %+d)", fill.instrument_id, fill.side,
                         fill.volume, fill.price, ctx.position(fill.instrument_id))
