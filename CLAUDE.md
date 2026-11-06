# CLAUDE.md - optiver-eth-2026

## Project Overview

Automated trading bot for **Optiver's coding competition** at ETH Zurich (6 Nov 2026). Teams trade on the **Optibook** exchange (https://2026-eth.optibook.net/, API docs at /docs) and are ranked by PnL and by how well the strategy is built. The infrastructure in `bot/` is strategy-agnostic; strategies are drop-in files in `strategies/`.

## Tech Stack

- **Python 3.9+** (tested on 3.9 and 3.15); standard library only
- **optibook**: Optiver's client (`synchronous_client.Exchange`, `exporter.Exporter`), installed on the platform. Not needed for `--mock`

## Project Structure

```
bot/                     infrastructure: change only when something is broken
  __main__.py            CLI: python -m bot ...
  engine.py              Engine (main loop, reconnect, kill switch, shutdown) + run()
  strategy.py            Strategy base class + Context (the only API strategies use)
  market.py              MarketData: books (ex-own), public trades, social posts, instruments
  account.py             Account: positions, cash, resting orders + owners, per-strategy Ledger
  gateway.py             OrderGateway (the single path to the exchange) + Throttle
  risk.py                RiskManager (pre-trade checks) + KillSwitch
  models.py              Book, Level, Order, BID/ASK, round_to_tick
  reporting.py           logging setup, status line, CSV exports
  settings.py            Settings: every limit/interval in one place
  loader.py              discovers strategies/<name>.py
  mock.py                MockExchange: offline dual listing (PHILIPS_A liquid, PHILIPS_B illiquid)
strategies/              one file = one strategy, named after the file; "_" files are ignored
  _template.py           start here
  example_signals.py     demo: publishes fair values in ctx.shared, logs news
  example_quoter.py      demo: quotes around fair value
tests/test_bot.py        unittest suite against the mock
exports/ exports_mock/   CSV output (git-ignored)
```

## Key Commands

```bash
python -m bot --list                                    # strategies and whether they load
python -m bot -s my_strategy --mock --duration 60       # offline
python -m bot -s my_strategy                            # live (needs optibook)
python -m bot -s signals,execution                      # combine; they tick in this order
python -m unittest discover -s tests                    # tests
# Ctrl+C (or SIGTERM): on_stop -> cancel all orders -> final PnL -> disconnect
```

## Code Style & Conventions

- **Strategy logic goes in `strategies/<name>.py` only.** Never edit `bot/` to add a strategy
- **Strategies only use `ctx`**: they never get the `Exchange` object, so they cannot skip risk checks or steal polled data
- **Per-tick flow**: account.sync → market.refresh → poll fills → kill switch → each `on_tick` in order → report
- **Errors**: programming mistakes raise `ValueError`; exchange or network failures return `None`/`False` and are logged. A strategy that raises has its orders cancelled and runs again next tick
- **Config** lives in `bot/settings.py` (`Settings`); strategy parameters are class attributes in the strategy file
- Code and comments in English; smallest change that solves the problem; no unrequested refactors

## Important Notes (from the Optibook docs)

- **One connection per username**: only one teammate can run the bot at a time per account
- All `get_*`/`poll_*` calls read the client's local cache (cheap); only insert/amend/delete go over the network
- **`poll_new_trades` / `poll_new_trade_ticks` / `poll_new_social_media_feeds` return each item once.** Only `Account`/`MarketData` call them; strategies read `ctx.fills`, `ctx.market.new_trades(id)`, `ctx.market.new_posts`
- Every client method asserts the client is connected, so a disconnect raises `AssertionError`. The gateway and engine catch it
- Sides `'bid'`/`'ask'`; order types `'limit'`/`'ioc'`; `amend_order` changes volume only (to change the price, delete and re-insert)
- `get_tradable_instruments()` excludes expired/paused instruments; the gateway refuses orders on them
- Instrument relationships: `instrument_type` (STOCK, STOCK_OPTION, STOCK_FUTURE, INDEX_TRACKING_ETF, INDEX_OPTION, INDEX_FUTURE), `base_instrument_id`, `strike`, `expiry`, `option_kind`, `index_constituents`, `etf_multiplier`, `etf_cash_comp`, `price_change_limit`
- Exporter appends to `exports/<file>.csv` with a UTC timestamp first, capped at 2 GB
- **To confirm at the tutorial** (placeholders in `settings.py`): position limits, message rate limit, scoring/valuation rule, whether the exchange rejects self-trades
