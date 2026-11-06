"""The main loop: connect, then every tick refresh -> kill switch -> strategies -> report."""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Callable

from bot.account import Account
from bot.gateway import OrderGateway, Throttle
from bot.loader import load_many
from bot.market import MarketData
from bot.mock import MockExchange
from bot.models import ASK, BID, IOC
from bot.reporting import LogLimiter, Reporter, make_exporter, setup_logging
from bot.risk import KillSwitch, RiskManager
from bot.settings import Settings, exchange_kwargs_from_env
from bot.strategy import Context, Strategy

log = logging.getLogger("bot.engine")
ENGINE = "engine"  # owner name of orders the engine sends itself (flattening)


class Engine:
    """Runs one or more strategies on a single exchange connection.

    Every tick:
      1. reconnect if needed (with back-off); the tick is skipped while disconnected
      2. refresh account and market state; if that fails the tick is skipped, so strategies
         never trade on stale data
      3. check the kill switch
      4. call each strategy's on_tick in the order given (so a strategy can use what an earlier
         one put in ctx.shared). If one raises, its orders are cancelled and it runs again next
         tick; the other strategies are unaffected
      5. record fills and market data, and log a status line every few seconds
    """

    def __init__(self, exchange, strategies: list[Strategy], settings: Settings | None = None,
                 reporter: Reporter | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        names = [s.name for s in strategies]
        if not names or len(set(names)) != len(names) or not all(names):
            raise ValueError(f"strategies need unique, non-empty names, got {names}")
        self.settings = s = settings or Settings()
        self.exchange = exchange
        self.strategies = list(strategies)
        self._clock, self._sleep = clock, sleep

        self.account = Account(exchange, clock)
        self.market = MarketData(exchange, s.trade_history, s.post_history)
        self.risk = RiskManager(s, self.account)
        self.gateway = OrderGateway(exchange, self.market, self.account, self.risk,
                                    Throttle(s.max_messages_per_second, clock, sleep))
        self.reporter = reporter or Reporter(status_interval=s.status_interval, clock=clock)
        self.kill_switch = KillSwitch(s.kill_pnl, s.kill_confirm_seconds)
        shared: dict = {}
        self.contexts = {n: Context(n, self.market, self.account, self.risk, self.gateway, shared)
                         for n in names}

        self.errors: dict[str, int] = defaultdict(int)  # on_tick failures per strategy
        self.stop_reason: str | None = None
        self._flatten_on_exit = s.flatten_on_stop
        self._started: list[Strategy] = []
        self._connected_once = False
        self._limiter = LogLimiter(5.0, clock)
        self._reconnect_at = 0.0
        self._reconnect_delay = 1.0

    # --- lifecycle ---

    def run(self, duration: float | None = None) -> None:
        """Trade until Ctrl+C, stop(), the kill switch, or `duration` seconds have passed."""
        try:
            self.start()
            deadline = None if duration is None else self._clock() + duration
            while self.stop_reason is None:
                began = self._clock()
                try:
                    self.step()
                except Exception:  # a bug in the bot itself: log it and keep going
                    if self._limiter.allow("step"):
                        log.exception("Unexpected error in the main loop")
                if deadline is not None and self._clock() >= deadline:
                    self.stop(f"ran for {duration:g}s")
                if self.stop_reason is None:
                    self._sleep(max(0.0, self.settings.tick_interval - (self._clock() - began)))
        except KeyboardInterrupt:
            self.stop("interrupted")
        finally:
            self.shutdown()

    def stop(self, reason: str = "stop requested") -> None:
        if self.stop_reason is None:
            self.stop_reason = reason

    def start(self) -> None:
        """Connect, clear leftover orders and start the strategies.

        Unlike later failures, errors here are raised: at start-up someone is watching, and
        silently running only some of the strategies would be worse than not starting.
        """
        log.info("Connecting to the exchange")
        self.exchange.connect()
        self._connected_once = True
        self._refresh()
        log.info("Connected: %d instruments, tradable: %s", len(self.market.instruments),
                 ", ".join(sorted(self.market.tradable)) or "none")
        for instrument in self.market.instruments.values():
            log.info("  %r", instrument)
        leftover = self.account.orders()
        if leftover and self.settings.cancel_orders_on_start:
            log.warning("Cancelling %d orders left over from an earlier run", len(leftover))
            self.gateway.delete_everything()
        for strategy in self.strategies:
            strategy.on_start(self.contexts[strategy.name])
            self._started.append(strategy)
        log.info("Running strategies: %s", ", ".join(s.name for s in self.strategies))

    def step(self) -> None:
        """One tick."""
        if not self._ensure_connected():
            return
        try:
            fills = self._refresh()
        except Exception:
            if self._limiter.allow("refresh"):
                log.exception("Could not refresh state from the exchange; skipping this tick")
            return
        self.reporter.on_tick(self.market, fills)

        pnl = self.account.pnl(self.market.mark)
        if self.kill_switch.update(pnl, self._clock()):
            log.error("KILL SWITCH: PnL %.2f stayed below %.2f; cancelling, flattening, stopping",
                      pnl, self.kill_switch.threshold)
            self._flatten_on_exit = True
            self.stop("kill switch")
            return

        self._run_strategies(fills)
        self.reporter.maybe_status(self.account, self.market, list(self.contexts), self.gateway)

    def shutdown(self) -> None:
        """on_stop for each started strategy, cancel every order, flatten if asked, disconnect.

        Each step is guarded so that one failing cannot skip the steps after it.
        """
        log.info("Stopping (%s)", self.stop_reason or "error during start-up")
        if not self._connected_once:
            return
        for strategy in self._started:
            self._guarded(f"{strategy.name}.on_stop", strategy.on_stop, self.contexts[strategy.name])
        if not self._guarded("reconnect", self._ensure_connected_now):
            log.error("Not connected: could not cancel orders. Check the exchange for resting orders!")
            return
        self._guarded("cancel all orders", self._cancel_everything)
        if self._flatten_on_exit:
            self._guarded("flatten positions", self._flatten)
        self._guarded("final status", self._final_status)
        self._guarded("disconnect", self.exchange.disconnect)

    # --- internals ---

    def _refresh(self) -> list:
        # Orders first, so books can hide them; fills last, so each fill can be credited to the
        # strategy whose order it was.
        self.account.sync()
        self.market.refresh(own_orders=self.account.orders())
        return self.account.poll_fills()

    def _run_strategies(self, fills: list) -> None:
        fills_by_owner = defaultdict(list)
        for owner, trade in fills:
            fills_by_owner[owner].append(trade)

        for strategy in self.strategies:
            ctx = self.contexts[strategy.name]
            ctx._begin_tick(fills_by_owner.get(strategy.name, []))
            began, throttled_before = self._clock(), self.gateway.throttled_seconds()
            try:
                strategy.on_tick(ctx)
            except Exception:
                self.errors[strategy.name] += 1
                if self._limiter.allow(("on_tick", strategy.name)):
                    log.exception("%s.on_tick failed (%d failures so far); cancelling its orders",
                                  strategy.name, self.errors[strategy.name])
                self.gateway.delete_all(strategy.name)  # never leave stale quotes behind a crash
            elapsed = self._clock() - began
            if elapsed > self.settings.tick_interval and self._limiter.allow(("slow", strategy.name)):
                throttled = self.gateway.throttled_seconds() - throttled_before
                log.warning("%s.on_tick took %.0f ms (tick interval %.0f ms); %.0f ms of that was "
                            "waiting on the message rate limit", strategy.name, elapsed * 1000,
                            self.settings.tick_interval * 1000, throttled * 1000)

    def _ensure_connected(self) -> bool:
        """Reconnect with exponential back-off. Never blocks: returns False until connected."""
        if self.exchange.is_connected():
            return True
        now = self._clock()
        if now < self._reconnect_at:
            return False
        log.warning("Disconnected from the exchange; reconnecting")
        try:
            self.exchange.connect()
        except Exception as exc:
            log.warning("Reconnect failed (%r); next attempt in %.0fs", exc, self._reconnect_delay)
            self._reconnect_at = now + self._reconnect_delay
            self._reconnect_delay = min(2 * self._reconnect_delay, self.settings.reconnect_delay_max)
            return False
        log.info("Reconnected")
        self._reconnect_delay = 1.0
        return self.exchange.is_connected()

    def _ensure_connected_now(self) -> bool:
        if not self.exchange.is_connected():
            self.exchange.connect()
        return self.exchange.is_connected()

    def _cancel_everything(self) -> None:
        self.account.sync()
        self.gateway.delete_everything()

    def _flatten(self, attempts: int = 5) -> None:
        """Trade every position back to zero with IOC orders that sweep the book."""
        for _ in range(attempts):
            self.reporter.on_tick(self.market, self._refresh())
            open_positions = {iid: pos for iid, pos in sorted(self.account.positions.items()) if pos}
            if not open_positions:
                log.info("All positions flat")
                return
            for instrument_id, position in open_positions.items():
                side = ASK if position > 0 else BID
                book = self.market.book(instrument_id)
                price = book.sweep_price(side, abs(position)) if book else None
                if price is None:
                    log.warning("Cannot flatten %s: nothing to trade against", instrument_id)
                    continue
                self.gateway.insert(ENGINE, instrument_id, side, price, abs(position), IOC)
            self._sleep(0.5)
        self._refresh()
        log.warning("Positions left after flattening: %s", {i: p for i, p in self.account.positions.items() if p})

    def _final_status(self) -> None:
        self.reporter.on_tick(self.market, self._refresh())
        self.reporter.maybe_status(self.account, self.market, list(self.contexts), self.gateway, force=True)

    def _guarded(self, what: str, fn: Callable, *args):
        try:
            return fn(*args)
        except Exception:
            log.exception("%s failed during shutdown", what)
            return None


def run(strategies: list[str], *, mock: bool = False, seed: int | None = None,
        duration: float | None = None, settings: Settings | None = None) -> Engine:
    """Load strategies by name and trade until Ctrl+C (or for `duration` seconds).

    Terminal: `python -m bot -s name1,name2`. Notebook: `from bot import run; run(["name1"])`.
    """
    if not logging.getLogger().handlers:  # e.g. called from a notebook
        setup_logging()
    settings = settings or Settings()
    loaded = load_many(strategies)
    exchange = MockExchange(seed=seed, position_limit=settings.position_limit) if mock else live_exchange()
    reporter = Reporter(make_exporter(mock), settings.record_market_data, settings.status_interval)
    engine = Engine(exchange, loaded, settings, reporter)
    engine.run(duration)
    return engine


def live_exchange():
    """optibook's Exchange, configured from OPTIBOOK_* environment variables if set."""
    try:
        from optibook.synchronous_client import Exchange
    except ImportError:
        raise RuntimeError("the optibook package is not installed here; run on the platform, "
                           "or use --mock to run offline") from None
    return Exchange(**exchange_kwargs_from_env())
