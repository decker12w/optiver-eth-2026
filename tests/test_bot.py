"""Tests for the bot infrastructure, run against the offline mock exchange.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import logging
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.disable(logging.CRITICAL)  # the scenarios below log warnings on purpose

from bot import ASK, BID, Book, Engine, Level, Settings, Strategy  # noqa: E402
from bot.gateway import Throttle  # noqa: E402
from bot.loader import StrategyLoadError, available, load, load_many  # noqa: E402
from bot.mock import MockExchange  # noqa: E402
from bot.models import Order, round_to_tick  # noqa: E402
from bot.reporting import COLUMNS, CsvExporter, Reporter  # noqa: E402

A, B = "PHILIPS_A", "PHILIPS_B"


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class Scripted(Strategy):
    """Runs whatever function the test assigns to `tick`."""

    def __init__(self, name: str, tick=None):
        self.name = name
        self.tick = tick or (lambda ctx: None)
        self.ticks = 0

    def on_tick(self, ctx) -> None:
        self.ticks += 1
        self.tick(ctx)


def make_engine(*strategies, mock_limit: int = 100, **settings):
    clock = FakeClock()
    exchange = MockExchange(seed=7, auto_step=False, position_limit=mock_limit, clock=clock.now)
    engine = Engine(exchange, list(strategies), Settings(**settings), clock=clock.now, sleep=clock.sleep)
    engine.start()
    return engine, exchange, clock


class ModelTests(unittest.TestCase):
    def test_round_to_tick_never_rounds_aggressively(self):
        self.assertEqual(round_to_tick(100.07, 0.1, BID), 100.0)
        self.assertEqual(round_to_tick(100.07, 0.1, ASK), 100.1)
        self.assertEqual(round_to_tick(100.1, 0.1, BID), 100.1)  # on-grid prices stay put
        self.assertEqual(round_to_tick(0.1 + 0.2, 0.1, BID), 0.3)  # float noise is ignored
        self.assertEqual(round_to_tick(12.34, 0.05, ASK), 12.35)

    def test_book_without_hides_own_volume(self):
        book = Book(A, bids=(Level(100.0, 10), Level(99.9, 5)), asks=(Level(100.1, 3),))
        mine = [Order(1, A, BID, 100.0, 10, "s"), Order(2, A, ASK, 100.1, 1, "s")]
        clean = book.without(mine)
        self.assertEqual(clean.best_bid, 99.9)
        self.assertEqual(clean.asks, (Level(100.1, 2),))

    def test_book_derived_prices(self):
        book = Book(A, bids=(Level(100.0, 30), Level(99.9, 50)), asks=(Level(100.2, 10), Level(100.3, 40)))
        self.assertAlmostEqual(book.mid, 100.1)
        self.assertAlmostEqual(book.spread, 0.2)
        self.assertAlmostEqual(book.microprice, (100.0 * 10 + 100.2 * 30) / 40)
        self.assertEqual(book.sweep_price(BID, 25), 100.3)
        self.assertEqual(book.sweep_price(ASK, 500), 99.9)  # thin book: last level
        self.assertIsNone(Book(A, bids=(Level(1.0, 1),)).mid)


class RiskTests(unittest.TestCase):
    def test_volume_is_clipped_to_position_limit_counting_resting_orders(self):
        engine, _, _ = make_engine(Scripted("s"), position_limit=10)
        ctx = engine.contexts["s"]
        first = ctx.buy(A, 90.0, 6)
        second = ctx.buy(A, 90.0, 50)
        self.assertEqual(engine.account.order(first).volume, 6)
        self.assertEqual(engine.account.order(second).volume, 4)
        self.assertIsNone(ctx.buy(A, 90.0, 1))
        self.assertEqual(engine.gateway.refusals["position limit"], 1)
        self.assertEqual(ctx.room(A, ASK), 10)

    def test_ioc_volume_counts_until_next_refresh(self):
        engine, exchange, _ = make_engine(Scripted("s"), position_limit=10)
        ctx = engine.contexts["s"]
        ask = engine.market.book(A).best_ask
        ctx.buy(A, ask, 8, ioc=True)
        self.assertEqual(ctx.room(A, BID), 2)  # fill not refreshed yet, still counted
        engine.step()
        self.assertEqual(ctx.position(A), 8)
        self.assertEqual(ctx.room(A, BID), 2)

    def test_never_trades_with_itself_across_strategies(self):
        engine, _, _ = make_engine(Scripted("maker"), Scripted("taker"))
        engine.contexts["maker"].sell(A, 150.0, 5)
        self.assertIsNone(engine.contexts["taker"].buy(A, 150.0, 5, ioc=True))
        self.assertEqual(engine.gateway.refusals["would trade with own order"], 1)

    def test_open_order_cap(self):
        engine, _, _ = make_engine(Scripted("s"), max_open_orders_per_instrument=3)
        ctx = engine.contexts["s"]
        ids = [ctx.buy(A, 50.0 + i, 1) for i in range(4)]
        self.assertIsNone(ids[3])
        self.assertEqual(len(ctx.orders(A)), 3)

    def test_position_limit_holds_under_aggressive_trading(self):
        def hammer(ctx):
            for iid in (A, B):
                book = ctx.market.book(iid)
                if book and book.is_two_sided:
                    ctx.buy(iid, book.sweep_price(BID, 30), 30, ioc=True)
                    ctx.quote(iid, bid=book.best_bid, ask=None, volume=20)

        engine, exchange, _ = make_engine(Scripted("s", hammer), position_limit=40, mock_limit=40)
        for _ in range(200):
            exchange.step()
            engine.step()
            for iid in (A, B):
                self.assertLessEqual(abs(engine.account.position(iid)), 40)
        self.assertEqual(engine.gateway.stats["rejected"], 0)  # we never even ask the exchange


class GatewayTests(unittest.TestCase):
    def test_programming_errors_raise(self):
        engine, _, _ = make_engine(Scripted("s"))
        ctx = engine.contexts["s"]
        with self.assertRaises(ValueError):
            ctx.buy("NOPE", 100.0, 1)
        with self.assertRaises(ValueError):
            ctx.buy(A, math.nan, 1)
        with self.assertRaises(ValueError):
            ctx.buy(A, 100.0, math.inf)
        with self.assertRaises(ValueError):
            engine.gateway.insert("s", A, "buy", 100.0, 1)

    def test_zero_volume_sends_nothing(self):
        engine, exchange, _ = make_engine(Scripted("s"))
        before = exchange.requests
        self.assertIsNone(engine.contexts["s"].buy(A, 100.0, 0))
        self.assertEqual(exchange.requests, before)

    def test_exchange_rejection_is_reported_not_raised(self):
        engine, _, _ = make_engine(Scripted("s"), mock_limit=5)  # exchange stricter than our settings
        self.assertIsNone(engine.contexts["s"].buy(A, 90.0, 10))
        self.assertEqual(engine.gateway.stats["rejected"], 1)

    def test_disconnect_during_order_is_reported_not_raised(self):
        engine, exchange, _ = make_engine(Scripted("s"))
        exchange.disconnect()
        self.assertIsNone(engine.contexts["s"].buy(A, 90.0, 1))
        self.assertEqual(engine.gateway.stats["errors"], 1)

    def test_strategies_cannot_touch_each_others_orders(self):
        engine, _, _ = make_engine(Scripted("a"), Scripted("b"))
        order_id = engine.contexts["a"].buy(A, 90.0, 1)
        self.assertEqual(engine.contexts["b"].orders(), [])
        with self.assertRaises(ValueError):
            engine.contexts["b"].cancel(order_id)

    def test_throttle_caps_message_rate(self):
        clock = FakeClock()
        throttle = Throttle(3, clock.now, clock.sleep)
        for _ in range(7):
            throttle.acquire()
        self.assertGreaterEqual(clock.t - 1000.0, 2.0)
        self.assertLessEqual(throttle.sent_last_second(), 3)


class QuoteTests(unittest.TestCase):
    def test_quote_only_sends_what_changed(self):
        engine, exchange, _ = make_engine(Scripted("s"))
        ctx = engine.contexts["s"]
        start = exchange.requests
        bid_id, ask_id = ctx.quote(A, bid=90.0, ask=110.0, volume=5)
        self.assertEqual(exchange.requests - start, 2)

        start = exchange.requests
        self.assertEqual(ctx.quote(A, bid=90.04, ask=110.0, volume=5), (bid_id, ask_id))  # same tick
        self.assertEqual(exchange.requests, start)

        new_bid, same_ask = ctx.quote(A, bid=90.5, ask=110.0, volume=5)
        self.assertNotEqual(new_bid, bid_id)
        self.assertEqual(same_ask, ask_id)
        self.assertEqual(exchange.requests - start, 2)  # one delete + one insert

        self.assertEqual(ctx.quote(A, bid=None, ask=None, volume=5), (None, None))
        self.assertEqual(ctx.orders(A), [])


class EngineTests(unittest.TestCase):
    def test_fills_go_to_the_strategy_that_traded(self):
        engine, _, _ = make_engine(Scripted("buyer"), Scripted("seller"))
        book = engine.market.book(A)
        engine.contexts["buyer"].buy(A, book.best_ask, 3, ioc=True)
        engine.contexts["seller"].sell(A, book.best_bid, 2, ioc=True)
        engine.step()
        self.assertEqual([t.volume for t in engine.contexts["buyer"].fills], [3])
        self.assertEqual([t.volume for t in engine.contexts["seller"].fills], [2])
        self.assertEqual(engine.contexts["buyer"].own_position(A), 3)
        self.assertEqual(engine.contexts["seller"].own_position(A), -2)
        self.assertEqual(engine.account.position(A), 1)

    def test_failing_strategy_is_contained(self):
        def crash(ctx):
            ctx.quote(A, bid=90.0, ask=None, volume=1)
            raise RuntimeError("bug in strategy")

        bad = Scripted("bad", crash)
        good = Scripted("good", lambda ctx: ctx.quote(A, bid=80.0, ask=None, volume=1))
        engine, _, _ = make_engine(bad, good)
        for _ in range(3):
            engine.step()
        self.assertEqual(engine.errors["bad"], 3)
        self.assertEqual(engine.contexts["bad"].orders(), [])  # its quote was pulled
        self.assertEqual(len(engine.contexts["good"].orders()), 1)
        self.assertEqual(good.ticks, 3)

    def test_reconnects_after_disconnect(self):
        strategy = Scripted("s")
        engine, exchange, clock = make_engine(strategy)
        exchange.disconnect()
        engine.step()  # reconnects; this tick's strategy call still happens on fresh data
        self.assertTrue(exchange.is_connected())
        self.assertEqual(strategy.ticks, 1)

    def test_skips_ticks_while_reconnect_keeps_failing(self):
        def refuse():
            raise ConnectionError("exchange down")

        strategy = Scripted("s")
        engine, exchange, clock = make_engine(strategy)
        exchange.disconnect()
        exchange.connect = refuse
        engine.step()
        engine.step()  # still inside the back-off window: no attempt, no tick
        self.assertEqual(strategy.ticks, 0)

    def test_kill_switch_waits_for_confirmation_then_flattens(self):
        def buy_once(ctx):
            if ctx.position(A) == 0 and not ctx.fills:
                ctx.buy(A, ctx.market.book(A).best_ask, 5, ioc=True)

        engine, exchange, clock = make_engine(Scripted("s", buy_once), kill_pnl=1e9, kill_confirm_seconds=1.0)
        engine.step()
        engine.step()
        self.assertEqual(engine.account.position(A), 5)
        clock.t += 0.5
        engine.step()
        self.assertIsNone(engine.stop_reason)
        clock.t += 0.6
        engine.step()
        self.assertEqual(engine.stop_reason, "kill switch")
        engine.shutdown()
        self.assertEqual(exchange._positions[A], 0)
        self.assertFalse(exchange.is_connected())

    def test_start_cancels_leftover_orders_and_shutdown_cancels_everything(self):
        clock = FakeClock()
        exchange = MockExchange(seed=1, auto_step=False, clock=clock.now)
        exchange.connect()
        exchange.insert_order(A, price=50.0, volume=1, side=BID)
        engine = Engine(exchange, [Scripted("s", lambda ctx: ctx.quote(A, 90.0, 110.0, 1))],
                        clock=clock.now, sleep=clock.sleep)
        engine.start()
        self.assertEqual(exchange.get_outstanding_orders(A), {})
        engine.step()
        self.assertEqual(len(exchange.get_outstanding_orders(A)), 2)
        engine.shutdown()
        exchange.connect()
        self.assertEqual(exchange.get_outstanding_orders(A), {})

    def test_on_start_failure_aborts_and_cleans_up(self):
        class Broken(Strategy):
            def on_start(self, ctx):
                ctx.buy(A, 90.0, 1)
                raise RuntimeError("bad config")

        broken = Broken()
        broken.name = "broken"
        clock = FakeClock()
        exchange = MockExchange(seed=1, auto_step=False, clock=clock.now)
        engine = Engine(exchange, [broken], clock=clock.now, sleep=clock.sleep)
        with self.assertRaises(RuntimeError):
            engine.run()
        exchange.connect()
        self.assertEqual(exchange.get_outstanding_orders(A), {})

    def test_fills_survive_a_refresh_that_fails_half_way(self):
        engine, exchange, _ = make_engine(Scripted("s"))
        ctx = engine.contexts["s"]
        ctx.buy(A, engine.market.book(A).best_ask, 2, ioc=True)
        ctx.buy(B, engine.market.book(B).best_ask, 1, ioc=True)
        real_poll, calls = exchange.poll_new_trades, []

        def disconnects_on_second_call(instrument_id):
            calls.append(instrument_id)
            if len(calls) == 2:
                raise AssertionError("Cannot call function until connected")
            return real_poll(instrument_id)

        exchange.poll_new_trades = disconnects_on_second_call
        engine.step()  # PHILIPS_A's fill was polled, then the refresh failed: tick skipped
        exchange.poll_new_trades = real_poll
        engine.step()
        self.assertEqual(sorted(t.volume for t in ctx.fills), [1, 2])

    def test_one_instrument_without_a_book_does_not_stall_the_others(self):
        quoted = []
        engine, exchange, _ = make_engine(Scripted("s", lambda ctx: quoted.append(ctx.market.mid(A))))
        real_book = exchange.get_last_price_book

        def no_book_for_b(instrument_id):
            if instrument_id == B:
                raise KeyError(instrument_id)
            return real_book(instrument_id)

        exchange.get_last_price_book = no_book_for_b
        engine.step()
        self.assertIsNone(engine.market.book(B))
        self.assertIsNotNone(quoted[0])

    def test_shared_state_flows_in_strategy_order(self):
        seen = []
        producer = Scripted("producer", lambda ctx: ctx.shared.__setitem__("fair", ctx.market.mid(A)))
        consumer = Scripted("consumer", lambda ctx: seen.append(ctx.shared.get("fair")))
        engine, _, _ = make_engine(producer, consumer)
        engine.step()
        self.assertEqual(seen, [engine.market.mid(A)])

    def test_keeps_trading_through_random_disconnects(self):
        def quote_inside_spread(ctx):  # PHILIPS_B is wide, so takers reach us first
            book = ctx.market.book(B)
            if book is not None and book.spread and book.spread > 0.25:
                ctx.quote(B, bid=book.best_bid + 0.1, ask=book.best_ask - 0.1, volume=2)

        strategy = Scripted("s", quote_inside_spread)
        engine, exchange, clock = make_engine(strategy)
        exchange._disconnect_probability = 0.02
        for _ in range(500):
            exchange.step()
            engine.step()
            clock.t += 0.1
        self.assertGreater(strategy.ticks, 400)
        self.assertGreater(engine.account.ledgers["s"].fills, 0)


class LoaderTests(unittest.TestCase):
    def test_discovers_strategies_and_ignores_underscore_files(self):
        names = available()
        self.assertIn("example_quoter", names)
        self.assertIn("example_signals", names)
        self.assertNotIn("_template", names)

    def test_load_names_the_strategy_after_its_file(self):
        self.assertEqual(load("example_quoter").name, "example_quoter")

    def test_bad_names_fail_clearly(self):
        with self.assertRaises(StrategyLoadError):
            load("does_not_exist")
        with self.assertRaises(StrategyLoadError):
            load_many(["example_quoter", "example_quoter"])


class ReportingTests(unittest.TestCase):
    def test_csv_files_get_a_header_and_timestamped_rows(self):
        engine, _, _ = make_engine(Scripted("s"))
        with tempfile.TemporaryDirectory() as folder:
            reporter = Reporter(CsvExporter(folder), record_market_data=True)
            book = engine.market.book(A)
            engine.contexts["s"].buy(A, book.best_ask, 1, ioc=True)
            engine.exchange.step()
            reporter.on_tick(engine.market, engine._refresh())
            reporter.maybe_status(engine.account, engine.market, ["s"], engine.gateway, force=True)
            with open(os.path.join(folder, "fills.csv")) as f:
                lines = f.read().splitlines()
            self.assertEqual(lines[0], ",".join(["timestamp"] + COLUMNS["fills.csv"]))
            self.assertIn(",s,PHILIPS_A,bid,", lines[1])
            self.assertTrue(os.path.exists(os.path.join(folder, "pnl.csv")))
            self.assertTrue(os.path.exists(os.path.join(folder, "books.csv")))


if __name__ == "__main__":
    unittest.main()
