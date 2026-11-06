"""Demo signal source: publishes a fair value per instrument in ctx.shared and logs news. Never trades."""
from bot import Context, Strategy


class ExampleSignals(Strategy):
    def on_tick(self, ctx: Context) -> None:
        for post in ctx.market.new_posts:
            ctx.log.info("NEWS: %s", post.post)

        fair_values = {}
        for instrument_id in ctx.market.tradable:
            book = ctx.market.book(instrument_id)
            if book is not None and book.is_two_sided:
                fair_values[instrument_id] = book.mid
        ctx.shared["fair_value"] = fair_values
