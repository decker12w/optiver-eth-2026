"""Logging setup, periodic status lines and CSV exports.

CSV files go to exports/ through optibook's Exporter when it is installed (the platform's
supported way to get data out), otherwise through CsvExporter, which behaves the same way.
Every row's first column is the UTC timestamp of the export.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Callable, Hashable

log = logging.getLogger("bot")

COLUMNS = {
    "fills.csv": ["strategy", "instrument", "side", "price", "volume", "order_id", "trade_id"],
    "pnl.csv": ["pnl", "cash", "positions", "strategy_pnl"],
    "books.csv": ["instrument", "bid", "bid_volume", "ask", "ask_volume"],
    "trades.csv": ["instrument", "price", "volume", "aggressor_side", "buyer", "seller", "trade_id"],
    "posts.csv": ["posted_at", "post"],
}


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,  # replace handlers a notebook may already have installed
    )


class LogLimiter:
    """Lets each message key through at most once per `interval` seconds.

    Repeated warnings at tick rate flood the terminal (and slow a notebook to a crawl), so
    anything that can fire every tick goes through one of these.
    """

    def __init__(self, interval: float = 5.0, clock: Callable[[], float] = time.monotonic):
        self._interval = interval
        self._clock = clock
        self._last: dict[Hashable, float] = {}

    def allow(self, key: Hashable) -> bool:
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < self._interval:
            return False
        self._last[key] = now
        return True


class CsvExporter:
    """Same behaviour as optibook.exporter.Exporter: appends rows to <folder>/<file> with a UTC
    timestamp prepended, and stops writing once the folder reaches `limit_mb`."""

    def __init__(self, folder: str = "exports", limit_mb: float = 2048):
        self.folder = folder
        self._limit_mb = limit_mb
        os.makedirs(folder, exist_ok=True)

    def export(self, data: dict[str, list[list[str]]]) -> None:
        if self._folder_size_mb() >= self._limit_mb:
            log.warning("exports folder is full (%s MB); not writing", self._limit_mb)
            return
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "")
        for filename, rows in data.items():
            with open(os.path.join(self.folder, filename), "a", newline="") as f:
                writer = csv.writer(f)
                for row in rows:
                    writer.writerow([timestamp] + list(row))

    def _folder_size_mb(self) -> float:
        total = 0
        for path, _dirs, files in os.walk(self.folder):
            total += sum(os.path.getsize(os.path.join(path, name)) for name in files)
        return total / (1024 * 1024)


def make_exporter(mock: bool):
    """optibook's Exporter for live runs; a separate folder for mock runs so data never mixes."""
    if mock:
        return CsvExporter("exports_mock")
    try:
        from optibook.exporter import Exporter
    except ImportError:
        return CsvExporter("exports")
    return Exporter()


class Reporter:
    """Writes fills (always) and market data (optional) every tick, and a status line + pnl row
    every `status_interval` seconds."""

    def __init__(self, exporter=None, record_market_data: bool = True,
                 status_interval: float = 5.0, clock: Callable[[], float] = time.monotonic):
        self._exporter = exporter
        self._folder = getattr(exporter, "folder", "exports")  # optibook's Exporter uses exports/
        self._record_market_data = record_market_data
        self._status_interval = status_interval
        self._clock = clock
        self._next_status = 0.0
        self._last_top: dict[str, tuple] = {}
        self._rows: dict[str, list[list[str]]] = {}
        self._limiter = LogLimiter(60.0, clock)

    def on_tick(self, market, fills: list) -> None:
        for owner, t in fills:
            log.info("FILL %s: %s %s %d @ %s", owner, t.instrument_id, t.side, t.volume, t.price)
            self._add("fills.csv", [owner, t.instrument_id, t.side, t.price, t.volume, t.order_id, t.trade_id])
        if self._record_market_data:
            self._record_market(market)
        self._flush()

    def maybe_status(self, account, market, strategy_names: list[str], gateway, force: bool = False) -> None:
        now = self._clock()
        if not force and now < self._next_status:
            return
        self._next_status = now + self._status_interval

        pnl = account.pnl(market.mark)
        positions = {iid: pos for iid, pos in sorted(account.positions.items()) if pos}
        names = list(strategy_names) + [n for n in account.ledgers if n not in strategy_names]
        by_strategy = {name: account.ledgers[name].pnl(market.mark) for name in names}
        refused = ", ".join(f"{reason} x{n}" for reason, n in gateway.refusals.most_common()) or "0"
        log.info(
            "PnL %s | cash %.2f | pos %s | %s | msgs %d/s | refused %s | rejected %d | errors %d",
            _money(pnl), account.cash, _positions(positions),
            " ".join(f"{n} {_money(p)} ({account.ledgers[n].fills} fills)" for n, p in by_strategy.items()),
            gateway.messages_last_second(), refused, gateway.stats["rejected"], gateway.stats["errors"],
        )
        self._add("pnl.csv", [None if pnl is None else round(pnl, 2), round(account.cash, 2), json.dumps(positions),
                              json.dumps({n: None if p is None else round(p, 2) for n, p in by_strategy.items()})])
        self._flush()

    def _record_market(self, market) -> None:
        for instrument_id in market.instruments:
            book = market.raw_book(instrument_id)
            if book is not None:
                top = (book.best_bid, book.bid_volume, book.best_ask, book.ask_volume)
                if top != self._last_top.get(instrument_id):
                    self._last_top[instrument_id] = top
                    self._add("books.csv", [instrument_id, *top])
            for t in market.new_trades(instrument_id):
                self._add("trades.csv", [instrument_id, t.price, t.volume, t.aggressor_side,
                                         t.buyer, t.seller, t.trade_id])
        for post in market.new_posts:
            self._add("posts.csv", [post.timestamp, post.post])

    def _add(self, filename: str, row: list) -> None:
        self._rows.setdefault(filename, []).append(["" if v is None else str(v) for v in row])

    def _flush(self) -> None:
        rows, self._rows = self._rows, {}
        if self._exporter is None or not rows:
            return
        try:
            for filename in rows:
                self._write_header_once(filename)
            self._exporter.export(rows)
        except Exception:  # never let disk trouble stop trading
            if self._limiter.allow("export"):
                log.exception("CSV export failed")

    def _write_header_once(self, filename: str) -> None:
        path = os.path.join(self._folder, filename)
        if not os.path.exists(path):
            os.makedirs(self._folder, exist_ok=True)
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(["timestamp"] + COLUMNS[filename])


def _money(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2f}"


def _positions(positions: dict[str, int]) -> str:
    return " ".join(f"{iid}={pos:+d}" for iid, pos in positions.items()) or "flat"
