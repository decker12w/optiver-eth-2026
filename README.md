# Optibook trading bot

Infrastructure for the Optiver coding competition at ETH Zurich. Strategies are drop-in files;
the bot handles connecting, market data, order routing, risk limits, rate limiting, reconnects,
PnL attribution and clean shutdown.

## Run

```bash
python -m bot --list                                     # what strategies exist
python -m bot -s example_quoter --mock --duration 30     # offline, no platform needed
python -m bot -s example_signals,example_quoter          # live, two strategies combined
python -m unittest discover -s tests                     # tests
```

From a notebook: `from bot import run; run(["my_strategy"])` (interrupt the kernel to stop).

Live mode uses `optibook.synchronous_client.Exchange()`. If the environment isn't pre-configured, set
`OPTIBOOK_HOST`, `OPTIBOOK_USERNAME`, `OPTIBOOK_PASSWORD` (and optionally `OPTIBOOK_INFO_PORT`,
`OPTIBOOK_EXEC_PORT`). Don't commit credentials.

## Write a strategy

1. Copy `strategies/_template.py` to `strategies/<name>.py` and rename the class.
2. Write `on_tick(self, ctx)`. Everything you can use is on `ctx`:

```python
from bot import BID, ASK, Context, Strategy

class MyStrategy(Strategy):
    EDGE = 0.2   # parameters are class attributes

    def on_tick(self, ctx: Context) -> None:
        book = ctx.market.book("PHILIPS_A")      # excludes our own orders
        if book is None or not book.is_two_sided:
            return ctx.cancel_all("PHILIPS_A")
        fair = book.microprice
        ctx.quote("PHILIPS_A", bid=fair - self.EDGE, ask=fair + self.EDGE, volume=5)
```

3. `python -m bot -s <name> --mock`. Then run it live.

| | |
|---|---|
| `ctx.market.book(id)` | `best_bid`, `best_ask`, `mid`, `microprice`, `spread`, `bids`, `asks`, `sweep_price(side, vol)` |
| `ctx.market.trades(id)` / `new_trades(id)` | public trades (history / since last tick) |
| `ctx.market.posts` / `new_posts` | social media feed |
| `ctx.market.instruments`, `tradable`, `by_type("STOCK_OPTION")` | instrument definitions and relationships |
| `ctx.fills` | this strategy's fills since last tick |
| `ctx.position(id)`, `ctx.room(id, side)`, `ctx.pnl` | account-wide (limits are account-wide) |
| `ctx.own_position(id)`, `ctx.own_pnl`, `ctx.orders(id)` | this strategy only |
| `ctx.quote(id, bid, ask, volume)` | keep one bid + one ask resting; sends only what changed |
| `ctx.buy/sell(id, price, volume, ioc=False)` | returns order id or `None` |
| `ctx.cancel(order_id)`, `ctx.cancel_all(id=None)`, `ctx.amend(order_id, volume)` | |
| `ctx.shared` | dict shared by all strategies |

## Combine strategies

`-s a,b,c` runs them on one connection, ticking in that order every tick. Risk limits and the message
budget are shared, and the bot never lets two strategies trade with each other. Fills and PnL are
attributed to the strategy whose order traded, and the status line shows PnL per strategy.

To split signal from execution, have an earlier strategy write to `ctx.shared` and a later one read it
(see `example_signals` + `example_quoter`).

## What the infrastructure guarantees

- **Risk before every order**: position limits use the worst case (position + all resting orders + IOCs this
  tick); orders are clipped, not rejected. Also: max order size, max open orders, no self-trades, tick rounding
  (bids down, asks up).
- **Rate limit**: all messages share a sliding-window budget. The bot waits rather than getting rejected, and
  logs when a strategy is slow because of it.
- **Failures stay contained**: an exception in `on_tick` cancels that strategy's orders and is retried next tick;
  other strategies keep trading. Exchange errors and disconnects never raise into strategy code.
- **No stale data**: if a refresh fails, the tick is skipped. Fills polled before the failure are delivered
  next tick.
- **Reconnects** with exponential back-off.
- **Clean start and stop**: orders left over from an earlier run are cancelled on start. On Ctrl+C/SIGTERM the bot
  runs `on_stop`, cancels everything, prints final PnL and disconnects.
- **Kill switch** (`Settings.kill_pnl`, off by default): if PnL stays below the threshold for a few seconds, the
  bot cancels, flattens and stops.

Tune limits in `bot/settings.py`. Values marked CONFIRM must be checked at the tutorial.

## Data for research

Each run appends to `exports/` (`exports_mock/` for mock runs). The first column is always the UTC timestamp:
`fills.csv`, `pnl.csv`, `books.csv` (top of book on change), `trades.csv` (public trades), `posts.csv`
(social media). Set `Settings.record_market_data = False` to record fills and PnL only.
