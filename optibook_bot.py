"""
Optibook bot skeleton - infra only. Strategy goes in `Strategy.on_tick`.

Run:
    python optibook_bot.py           # real exchange if `optibook` is installed, else mock
    python optibook_bot.py --mock    # force the offline mock exchange

Everything here (state refresh, risk checks, order helpers, PnL logging,
kill switch, reconnect) is strategy-agnostic. Confirm the CONFIG values
during the tutorial (position limit, rate limits, instruments).
"""
import argparse
import logging
import math
import random
import time
from types import SimpleNamespace as NS

try:
    from optibook.synchronous_client import Exchange
    HAVE_OPTIBOOK = True
except ImportError:
    HAVE_OPTIBOOK = False

try:
    from optibook.exporter import Exporter
except ImportError:
    Exporter = None

# ============================== CONFIG ==============================
POSITION_LIMIT = 100        # per instrument, abs. CONFIRM in tutorial
MAX_ORDER_VOLUME = 50       # safety cap per order
MAX_MSGS_PER_SEC = 20       # self-imposed throttle; adjust if exchange has one
LOOP_SLEEP = 0.10           # seconds between iterations
LOG_EVERY = 5.0             # seconds between PnL/position logs
KILL_PNL = -5_000           # stop trading + flatten below this PnL
EXCHANGE_KWARGS = {}        # e.g. dict(host=..., username=..., password=...) if not auto-configured

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
logging.getLogger("client").setLevel(logging.ERROR)  # silence optibook's own chatter


# ============================== CLIENT WRAPPER ==============================
class Client:
    """Thin layer over optibook Exchange: cached state + safe order helpers."""

    def __init__(self, exchange):
        self.e = exchange
        self.instruments = {}       # id -> Instrument
        self.books = {}             # id -> PriceBook
        self.positions = {}         # id -> int
        self.cash = 0.0
        self.fills = []             # all own trades (Trade)
        self.ticks = {}             # id -> list of recent public TradeTick
        self.feeds = []             # all social media posts
        self._msg_times = []

    # ---------- connection ----------
    def connect(self):
        self.e.connect()
        self.instruments = self.e.get_instruments()
        log.info("Connected. Instruments: %s", list(self.instruments))
        for iid, ins in self.instruments.items():
            log.info("  %s tick=%s type=%s", iid, ins.tick_size, getattr(ins, "instrument_type", None))

    def ensure_connected(self):
        if not self.e.is_connected():
            log.warning("Disconnected - reconnecting")
            self.connect()

    # ---------- state ----------
    def refresh(self):
        """Pull latest books/positions/cash. Returns (new_fills, new_ticks, new_feeds)."""
        tradable = self.e.get_tradable_instruments()
        new_fills, new_ticks = [], {}
        for iid in self.instruments:
            if iid in tradable:
                self.books[iid] = self.e.get_last_price_book(iid)
            f = self.e.poll_new_trades(iid)
            t = self.e.poll_new_trade_ticks(iid)
            new_fills += f
            new_ticks[iid] = t
            self.ticks.setdefault(iid, []).extend(t)
            self.ticks[iid] = self.ticks[iid][-500:]
        self.fills += new_fills
        self.positions = self.e.get_positions()
        self.cash = self.e.get_cash()
        try:
            new_feeds = self.e.poll_new_social_media_feeds()
        except Exception:
            new_feeds = []
        self.feeds += new_feeds
        return new_fills, new_ticks, new_feeds

    def tradable(self, iid):
        return iid in self.books and self.best_bid(iid) is not None and self.best_ask(iid) is not None

    # ---------- book helpers ----------
    def best_bid(self, iid):
        b = self.books.get(iid)
        return b.bids[0].price if b and b.bids else None

    def best_ask(self, iid):
        b = self.books.get(iid)
        return b.asks[0].price if b and b.asks else None

    def bid_vol(self, iid):
        b = self.books.get(iid)
        return b.bids[0].volume if b and b.bids else 0

    def ask_vol(self, iid):
        b = self.books.get(iid)
        return b.asks[0].volume if b and b.asks else 0

    def mid(self, iid):
        bb, ba = self.best_bid(iid), self.best_ask(iid)
        return (bb + ba) / 2 if bb is not None and ba is not None else None

    def microprice(self, iid):
        """Volume-weighted mid: leans toward the side with less volume."""
        bb, ba, bv, av = self.best_bid(iid), self.best_ask(iid), self.bid_vol(iid), self.ask_vol(iid)
        if bb is None or ba is None or bv + av == 0:
            return self.mid(iid)
        return (bb * av + ba * bv) / (bv + av)

    def spread(self, iid):
        bb, ba = self.best_bid(iid), self.best_ask(iid)
        return ba - bb if bb is not None and ba is not None else None

    def round_price(self, iid, price, side):
        """Round to tick: bids down, asks up (never more aggressive than intended)."""
        tick = self.instruments[iid].tick_size
        n = price / tick
        n = math.floor(n + 1e-9) if side == "bid" else math.ceil(n - 1e-9)
        return round(n * tick, 10)

    # ---------- risk ----------
    def position(self, iid):
        return self.positions.get(iid, 0)

    def room(self, iid, side):
        """Max volume we can still add on `side` without breaching the limit,
        counting resting orders on the same side as if they all fill."""
        pos = self.position(iid)
        resting = sum(o.volume for o in self.e.get_outstanding_orders(iid).values() if o.side == side)
        if side == "bid":
            return max(0, POSITION_LIMIT - pos - resting)
        return max(0, POSITION_LIMIT + pos - resting)

    # ---------- orders ----------
    def _throttle(self):
        now = time.time()
        self._msg_times = [t for t in self._msg_times if now - t < 1.0]
        if len(self._msg_times) >= MAX_MSGS_PER_SEC:
            time.sleep(1.0 - (now - self._msg_times[0]))
        self._msg_times.append(time.time())

    def order(self, iid, side, price, volume, ioc=False):
        """Risk-checked insert. Returns order_id or None."""
        volume = int(min(volume, MAX_ORDER_VOLUME, self.room(iid, side)))
        if volume <= 0:
            return None
        price = self.round_price(iid, price, side)
        self._throttle()
        r = self.e.insert_order(iid, price=price, volume=volume, side=side,
                                order_type="ioc" if ioc else "limit")
        if not r.success:
            log.warning("Order rejected %s %s %s@%s: %s", iid, side, volume, price, r.error_reason)
            return None
        return r.order_id

    def buy(self, iid, price, volume, ioc=False):
        return self.order(iid, "bid", price, volume, ioc)

    def sell(self, iid, price, volume, ioc=False):
        return self.order(iid, "ask", price, volume, ioc)

    def cancel(self, iid, order_id):
        self._throttle()
        return self.e.delete_order(iid, order_id=order_id).success

    def cancel_all(self, iid=None):
        for i in ([iid] if iid else self.instruments):
            if self.e.get_outstanding_orders(i):
                self._throttle()
                self.e.delete_orders(i)

    def quote(self, iid, bid_price, ask_price, size):
        """Keep one bid and one ask on iid. A side is only cancelled/reinserted when
        its target price changed, so we save rate limit and keep queue priority.
        Pass None for a side to pull it. Returns (bid_order_id, ask_order_id)."""
        out = self.e.get_outstanding_orders(iid)
        ids = {}
        for side, px in (("bid", bid_price), ("ask", ask_price)):
            mine = [o for o in out.values() if o.side == side]
            target = self.round_price(iid, px, side) if px is not None else None
            if target is not None and len(mine) == 1 and abs(mine[0].price - target) < 1e-9:
                ids[side] = mine[0].order_id      # unchanged: leave it resting
                continue
            for o in mine:
                self.cancel(iid, o.order_id)
            ids[side] = self.order(iid, side, px, size) if px is not None else None
        return ids["bid"], ids["ask"]

    def take(self, iid, side, volume, slippage_ticks=0):
        """Aggress the book with an IOC at best price (+/- slippage)."""
        tick = self.instruments[iid].tick_size
        if side == "bid" and self.best_ask(iid) is not None:
            return self.buy(iid, self.best_ask(iid) + slippage_ticks * tick, volume, ioc=True)
        if side == "ask" and self.best_bid(iid) is not None:
            return self.sell(iid, self.best_bid(iid) - slippage_ticks * tick, volume, ioc=True)
        return None

    def flatten(self, iid, slippage_ticks=5):
        pos = self.position(iid)
        if pos > 0:
            self.take(iid, "ask", pos, slippage_ticks)
        elif pos < 0:
            self.take(iid, "bid", -pos, slippage_ticks)

    # ---------- pnl ----------
    def pnl(self):
        """Cash + positions valued at mid (more stable than last-trade valuation)."""
        vals = {i: m for i in self.instruments if (m := self.mid(i)) is not None}
        try:
            return self.e.get_pnl(vals)
        except Exception:
            return self.cash + sum(p * vals.get(i, 0) for i, p in self.positions.items())


# ============================== LOGGING ==============================
class Recorder:
    def __init__(self):
        self.exp = Exporter() if Exporter else None
        self.last = 0.0

    def maybe_log(self, c: Client, new_fills):
        if self.exp and new_fills:
            self.exp.export({"fills.csv": [[t.instrument_id, t.side, str(t.price), str(t.volume)] for t in new_fills]})
        now = time.time()
        if now - self.last < LOG_EVERY:
            return
        self.last = now
        pnl = c.pnl()
        pos = {i: p for i, p in c.positions.items() if p}
        log.info("PnL=%.2f cash=%.2f pos=%s fills=%d", pnl, c.cash, pos, len(c.fills))
        if self.exp:
            self.exp.export({"pnl.csv": [[f"{pnl:.2f}", f"{c.cash:.2f}", str(pos)]]})


# ============================== STRATEGY ==============================
class Strategy:
    """Put the trading logic here. Called once per loop iteration.

    Useful: c.instruments, c.tradable(i), c.best_bid/ask(i), c.mid(i), c.microprice(i),
    c.spread(i), c.position(i), c.room(i, side), c.buy/sell(i, px, vol, ioc=),
    c.quote(i, bid, ask, size), c.take(i, side, vol), c.cancel_all(i), c.flatten(i),
    c.ticks[i] (public trades), c.feeds (social media), c.pnl().
    """

    def on_start(self, c: Client):
        pass

    def on_tick(self, c: Client, new_fills, new_ticks, new_feeds):
        for post in new_feeds:
            log.info("FEED: %s", getattr(post, "post", post))
        # TODO: strategy

    def on_stop(self, c: Client):
        c.cancel_all()


# ============================== MAIN LOOP ==============================
def run(exchange, strategy: Strategy):
    c = Client(exchange)
    c.connect()
    rec = Recorder()
    strategy.on_start(c)
    killed = False
    try:
        while True:
            t0 = time.time()
            try:
                c.ensure_connected()
                fills, ticks, feeds = c.refresh()
                for f in fills:
                    log.info("FILL %s %s %d @ %s", f.instrument_id, f.side, f.volume, f.price)
                if not killed and c.pnl() < KILL_PNL:
                    log.error("KILL SWITCH: PnL %.2f < %s - flattening", c.pnl(), KILL_PNL)
                    killed = True
                    c.cancel_all()
                    for i in c.instruments:
                        c.flatten(i)
                if not killed:
                    strategy.on_tick(c, fills, ticks, feeds)
                rec.maybe_log(c, fills)
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Loop error - continuing")
                time.sleep(1)
            time.sleep(max(0.0, LOOP_SLEEP - (time.time() - t0)))
    except KeyboardInterrupt:
        log.info("Stopping - cancelling all orders")
    finally:
        strategy.on_stop(c)
        log.info("Final PnL=%.2f pos=%s", c.pnl(), c.positions)


# ============================== MOCK EXCHANGE ==============================
class MockExchange:
    """Offline stand-in with the same method names/return shapes as optibook.

    Dual-listing setup: both instruments track the same fair value (+ small noise).
      STOCK_A - liquid: tight spread, deep book, IOCs fill in full.
      STOCK_B - illiquid: wide spread, thin book, IOCs fill only up to MAX_IOC.
    Our resting limit orders fill when (a) the market moves through them, or
    (b) a random taker arrives and our price is at or better than the market's best.
    `ops` counts inserts/cancels so you can check rate-limit usage."""

    SPECS = {
        "STOCK_A": dict(half_spread=0.1, taker_prob=0.30, depth=(80, 200), max_ioc=10_000),
        "STOCK_B": dict(half_spread=0.5, taker_prob=0.15, depth=(5, 20), max_ioc=20),
    }

    def __init__(self, tick=0.1):
        self.tick = tick
        self.specs = self.SPECS
        self.ids = tuple(self.specs)
        self.fair = 100.0
        self.offset = {i: 0.0 for i in self.ids}
        self.pos = {i: 0 for i in self.ids}
        self.cash = 0.0
        self.orders = {i: {} for i in self.ids}
        self.new_trades = {i: [] for i in self.ids}
        self.next_id = 1
        self.ops = 0
        self.connected = False

    # ---------- price model ----------
    def _r(self, x):
        return round(round(x / self.tick) * self.tick, 10)

    def _bb(self, i):
        return self._r(self.fair + self.offset[i] - self.specs[i]["half_spread"])

    def _ba(self, i):
        return self._r(self.fair + self.offset[i] + self.specs[i]["half_spread"])

    def _step(self):
        self.fair += random.gauss(0, 0.03)
        for i, s in self.specs.items():
            self.offset[i] = 0.8 * self.offset[i] + random.gauss(0, 0.02)
            bb, ba = self._bb(i), self._ba(i)
            # (a) market moved through our resting orders
            for o in list(self.orders[i].values()):
                if (o.side == "bid" and ba <= o.price) or (o.side == "ask" and bb >= o.price):
                    self._fill_resting(i, o, o.volume)
            # (b) random taker hits the best price; we fill first if we are at/inside it
            if random.random() < s["taker_prob"]:
                vol = random.randint(1, 10)
                if random.random() < 0.5:   # taker buys -> lifts asks
                    hit = sorted((o for o in self.orders[i].values() if o.side == "ask" and o.price <= ba),
                                 key=lambda o: o.price)
                else:                       # taker sells -> hits bids
                    hit = sorted((o for o in self.orders[i].values() if o.side == "bid" and o.price >= bb),
                                 key=lambda o: -o.price)
                for o in hit:
                    if vol <= 0:
                        break
                    q = min(vol, o.volume)
                    self._fill_resting(i, o, q)
                    vol -= q

    # ---------- fills ----------
    def _trade(self, i, side, price, vol, oid):
        sgn = 1 if side == "bid" else -1
        self.pos[i] += sgn * vol
        self.cash -= sgn * vol * price
        self.new_trades[i].append(NS(timestamp=time.time(), order_id=oid, trade_id=oid, instrument_id=i,
                                     price=price, volume=vol, side=side))

    def _fill_resting(self, i, o, q):
        self._trade(i, o.side, o.price, q, o.order_id)
        o.volume -= q
        if o.volume <= 0:
            self.orders[i].pop(o.order_id, None)

    # ---------- optibook interface ----------
    def connect(self): self.connected = True
    def disconnect(self): self.connected = False
    def is_connected(self): return self.connected

    def get_instruments(self):
        return {i: NS(instrument_id=i, tick_size=self.tick, instrument_type="STOCK") for i in self.ids}

    def get_tradable_instruments(self):
        self._step()                # called once per Client.refresh() -> one market step per loop
        return self.get_instruments()

    def get_last_price_book(self, i):
        bb, ba, (lo, hi) = self._bb(i), self._ba(i), self.specs[i]["depth"]
        return NS(instrument_id=i, timestamp=time.time(),
                  bids=[NS(price=self._r(bb - k * self.tick), volume=random.randint(lo, hi)) for k in range(3)],
                  asks=[NS(price=self._r(ba + k * self.tick), volume=random.randint(lo, hi)) for k in range(3)])

    def insert_order(self, i, *, price, volume, side, order_type="limit"):
        self.ops += 1
        oid = self.next_id
        self.next_id += 1
        if order_type == "ioc":
            best = self._ba(i) if side == "bid" else self._bb(i)
            if (side == "bid" and price >= best) or (side == "ask" and price <= best):
                self._trade(i, side, best, min(volume, self.specs[i]["max_ioc"]), oid)
        else:
            self.orders[i][oid] = NS(order_id=oid, instrument_id=i, price=price, volume=volume, side=side)
        return NS(success=True, order_id=oid, error_reason=None)

    def delete_order(self, i, *, order_id):
        self.ops += 1
        ok = self.orders[i].pop(order_id, None) is not None
        return NS(success=ok, error_reason=None if ok else "unknown order")

    def delete_orders(self, i):
        self.ops += 1
        self.orders[i].clear()

    def get_outstanding_orders(self, i): return dict(self.orders[i])

    def poll_new_trades(self, i):
        t, self.new_trades[i] = self.new_trades[i], []
        return t

    def poll_new_trade_ticks(self, i): return []
    def poll_new_social_media_feeds(self): return []
    def get_positions(self): return dict(self.pos)
    def get_cash(self): return self.cash

    def get_pnl(self, valuations=None):
        return self.cash + sum(p * (valuations or {}).get(i, self.fair) for i, p in self.pos.items())


# ============================== ENTRY ==============================
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    args = ap.parse_args()
    use_mock = args.mock or not HAVE_OPTIBOOK
    log.info("Using %s exchange", "MOCK" if use_mock else "REAL")
    ex = MockExchange() if use_mock else Exchange(**EXCHANGE_KWARGS)
    run(ex, Strategy())