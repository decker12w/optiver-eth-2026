# CLAUDE.md - optiver-eth-2026

## Project Overview

Automated trading bot for **Optiver's coding competition** at ETH Zurich (6 Nov 2026). Teams trade on the **Optibook** exchange (https://2026-eth.optibook.net/) and are ranked by PnL. Infra is pre-built; on event day only the strategy is written.

## Tech Stack

- **Python 3.10+** — single-file bot
- **optibook** — Optiver's exchange client (`synchronous_client.Exchange`, `exporter.Exporter`), provided by the platform
- **Standard library only** otherwise (no extra dependencies)

## Project Structure

```
optibook_bot.py                # Everything lives here
  CONFIG                       # Limits, throttles, kill switch, exchange kwargs (top of file)
  Client                       # Wrapper over Exchange: cached state + risk-checked order helpers
  Recorder                     # PnL/position logging, CSV export via Exporter
  Strategy                     # Trading logic — on_start / on_tick / on_stop
  run()                        # Main loop: reconnect → refresh → kill switch → on_tick → log
  MockExchange                 # Offline exchange with the same interface (STOCK_A, STOCK_B)
exports/                       # CSVs written by Exporter (fills.csv, pnl.csv)
CLAUDE.md
```

## Key Commands

```bash
python optibook_bot.py --mock  # Run against the offline mock exchange
python optibook_bot.py         # Run against the real exchange (needs `optibook` installed)
# Ctrl+C                       # Stop: cancels all outstanding orders, prints final PnL
```

## Code Style & Conventions

- **Strategy goes in `Strategy.on_tick` only** — infra changes only when something is broken
- **All orders go through `Client`** (`buy`, `sell`, `quote`, `take`, `flatten`, `cancel_all`) — never call `c.e.insert_order` directly, it bypasses position-limit checks
- **Prices** are rounded to tick by `Client.round_price` (bids down, asks up)
- **Config** values live as constants at the top of `optibook_bot.py`
- **Language**: code and comments in English
- **Changes**: smallest change that solves the problem; no unrequested refactors

## Important Notes

- **One connection per username** — only one teammate can run the bot at a time per account
- **Sides**: `'bid'` = buy, `'ask'` = sell. **Order types**: `'limit'` or `'ioc'`
- **`insert_order`** returns `success`, `order_id`, `error_reason` — always check `success`
- **`amend_order`** changes volume only; to change price, delete and reinsert
- **`poll_new_trades` / `poll_new_trade_ticks`** return only new items per instrument since the last call — `Client.refresh()` already calls them once per loop; read results from `c.fills`, `c.ticks`
- **PnL**: `get_pnl()` without valuations uses the last public trade (noisy); `Client.pnl()` passes mids instead
- **Instrument types**: `STOCK`, `STOCK_FUTURE`, `STOCK_OPTION`, `INDEX_FUTURE`, `INDEX_OPTION`, `INDEX_TRACKING_ETF` — expect relationships via `base_instrument_id`, `index_constituents`, `etf_multiplier`, `strike`, `expiry`
- **Social media feed** (`poll_new_social_media_feeds`) may drive price moves
- **Kill switch**: below `KILL_PNL` the bot cancels everything, flattens and stops trading
- **To confirm at tutorial**: position limits, exchange rate limits, how `Exchange()` is configured, instrument relationships, scoring rule
