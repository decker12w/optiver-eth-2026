"""<One line: what this strategy does. Shown by `python -m bot --list`.>

How to use this template:
  1. Copy it to strategies/<name>.py (no leading underscore) and rename the class.
  2. Run it:        python -m bot -s <name> --mock      (offline)
                    python -m bot -s <name>             (live)
  3. Combine it:    python -m bot -s <signal_strategy>,<name>
     Strategies tick in the order given. An earlier one can publish values in ctx.shared for
     later ones to read.

What a strategy can use (see bot/strategy.py for details):
  ctx.market.book(id)      Book without our own orders: best_bid, best_ask, mid, microprice,
                           spread, bid_volume, ask_volume, bids, asks, sweep_price(side, volume)
  ctx.market.trades(id)    recent public trades; ctx.market.new_trades(id) only the new ones
  ctx.market.new_posts     social media posts since the last tick (.post is the text)
  ctx.market.instruments   all instruments (tick_size, instrument_type, base_instrument_id, ...)
  ctx.fills                this strategy's own fills since the last tick
  ctx.position(id)         account-wide position; ctx.room(id, BID/ASK) = lots left under the limit
  ctx.quote(id, bid, ask, volume)       keep one bid + one ask resting, sends only what changed
  ctx.buy / ctx.sell(id, price, volume, ioc=False)
  ctx.cancel(order_id), ctx.cancel_all(id=None), ctx.orders(id)
"""
from bot import ASK, BID, Context, Strategy  # noqa: F401  (BID/ASK used by most strategies)


class Template(Strategy):
    # Parameters: plain class attributes, so tuning one is a one-line change.
    INSTRUMENT = "PHILIPS_A"
    VOLUME = 5

    def on_start(self, ctx: Context) -> None:
        ctx.log.info("tradable: %s", sorted(ctx.market.tradable))

    def on_tick(self, ctx: Context) -> None:
        book = ctx.market.book(self.INSTRUMENT)
        if book is None or not book.is_two_sided:
            ctx.cancel_all(self.INSTRUMENT)  # no reliable price: don't leave quotes out
            return

        for fill in ctx.fills:
            ctx.log.info("filled %s %d @ %.2f", fill.side, fill.volume, fill.price)

        # Decide, then act. For example:
        #   ctx.quote(self.INSTRUMENT, bid=fair - edge, ask=fair + edge, volume=self.VOLUME)
        #   ctx.buy(self.INSTRUMENT, price=book.best_ask, volume=1, ioc=True)

    def on_stop(self, ctx: Context) -> None:
        pass  # the engine cancels this strategy's orders after this
