"""What a strategy is (Strategy) and everything it may touch (Context)."""
from __future__ import annotations

import logging

from bot.account import Account
from bot.gateway import OrderGateway
from bot.market import MarketData
from bot.models import ASK, BID, IOC, LIMIT, Order, round_to_tick, same_price
from bot.risk import RiskManager


class Strategy:
    """Base class for strategies.

    Put one subclass in strategies/<name>.py and run it with `python -m bot -s <name>`.
    Parameters are plain class attributes. The engine catches exceptions from every hook:
    a failing on_tick gets the strategy's orders cancelled and is retried on the next tick.
    """

    name: str = ""  # set by the loader to the file name

    def on_start(self, ctx: Context) -> None:
        """Called once after connecting, before the first tick."""

    def on_tick(self, ctx: Context) -> None:
        """Called every tick with fresh data. The trading logic goes here."""

    def on_stop(self, ctx: Context) -> None:
        """Called once on shutdown. The engine cancels this strategy's orders afterwards."""


class Context:
    """A strategy's handle on the bot. Each strategy gets its own.

    Market     ctx.market          books, public trades, social posts, instruments
    Fills      ctx.fills           this strategy's own fills since the previous tick
    Account    ctx.position(id), ctx.positions, ctx.cash, ctx.pnl, ctx.room(id, side)
               account-wide (all strategies), because limits and scoring are account-wide
    Own        ctx.orders(id), ctx.own_position(id), ctx.own_pnl
               only what this strategy did
    Trade      ctx.buy, ctx.sell, ctx.quote, ctx.amend, ctx.cancel, ctx.cancel_all
               every order passes the risk checks; it acts on this strategy's own orders only
    Misc       ctx.shared (dict shared by all strategies), ctx.log, ctx.round_price

    Positions, cash and books are as of the start of the tick; ctx.orders() is updated as you trade.
    """

    def __init__(self, name: str, market: MarketData, account: Account, risk: RiskManager,
                 gateway: OrderGateway, shared: dict):
        self.name = name
        self.market = market
        self.shared = shared
        self.log = logging.getLogger(f"strategy.{name}")
        self._account = account
        self._risk = risk
        self._gateway = gateway
        self._fills: list = []

    def _begin_tick(self, fills: list) -> None:
        self._fills = fills

    # --- data ---

    @property
    def fills(self) -> list:
        """This strategy's fills since the previous tick (optibook Trade objects)."""
        return self._fills

    # --- account-wide ---

    def position(self, instrument_id: str) -> int:
        return self._account.position(instrument_id)

    @property
    def positions(self) -> dict[str, int]:
        return dict(self._account.positions)

    @property
    def cash(self) -> float:
        return self._account.cash

    @property
    def pnl(self) -> float | None:
        """Account PnL with positions valued at the mid (excluding our quotes); None if unknown."""
        return self._account.pnl(self.market.mark)

    def room(self, instrument_id: str, side: str) -> int:
        """Lots that can still be bought (BID) or sold (ASK) before hitting the position limit,
        counting every resting order on the account as if it filled."""
        return self._risk.room(instrument_id, side)

    # --- this strategy only ---

    def orders(self, instrument_id: str | None = None) -> list[Order]:
        return self._account.orders(instrument_id, owner=self.name)

    def own_position(self, instrument_id: str) -> int:
        return self._account.ledgers[self.name].positions.get(instrument_id, 0)

    @property
    def own_pnl(self) -> float | None:
        return self._account.ledgers[self.name].pnl(self.market.mark)

    # --- trading ---

    def buy(self, instrument_id: str, price: float, volume: int, ioc: bool = False) -> int | None:
        """Buy up to `volume` at `price` or better. Returns the order id, or None if not sent."""
        return self._gateway.insert(self.name, instrument_id, BID, price, volume, IOC if ioc else LIMIT)

    def sell(self, instrument_id: str, price: float, volume: int, ioc: bool = False) -> int | None:
        """Sell up to `volume` at `price` or better. Returns the order id, or None if not sent."""
        return self._gateway.insert(self.name, instrument_id, ASK, price, volume, IOC if ioc else LIMIT)

    def quote(self, instrument_id: str, bid: float | None, ask: float | None, volume: int,
              ask_volume: int | None = None) -> tuple[int | None, int | None]:
        """Keep exactly one resting bid and one resting ask on `instrument_id`.

        Call it every tick with the prices you want; it sends only what changed. A side priced
        None is pulled. An existing order stays put (keeping its queue position) while its price
        is unchanged and its remaining volume is at most the target; otherwise it is replaced.
        `volume` sizes both sides unless `ask_volume` is given.
        Returns (bid_order_id, ask_order_id) of the orders now resting (None where there is none).
        """
        tick = self.market.tick_size(instrument_id)
        targets = {
            BID: (None if bid is None else round_to_tick(bid, tick, BID), volume),
            ASK: (None if ask is None else round_to_tick(ask, tick, ASK),
                  volume if ask_volume is None else ask_volume),
        }
        resting: dict[str, int | None] = {BID: None, ASK: None}
        stale = []
        for side, (price, size) in targets.items():
            mine = [o for o in self.orders(instrument_id) if o.side == side]
            if (price is not None and len(mine) == 1 and same_price(mine[0].price, price)
                    and 0 < mine[0].volume <= size):
                resting[side] = mine[0].order_id
            else:
                stale.extend(mine)

        for order in stale:  # cancel before inserting, so new quotes never cross old ones
            self._gateway.delete(self.name, order.order_id)
        for side, (price, size) in targets.items():
            if resting[side] is None and price is not None:
                resting[side] = self._gateway.insert(self.name, instrument_id, side, price, size)
        return resting[BID], resting[ASK]

    def amend(self, order_id: int, volume: int) -> bool:
        """Change the volume of a resting order (to change the price: cancel and re-insert)."""
        return self._gateway.amend(self.name, order_id, volume)

    def cancel(self, order_id: int) -> bool:
        return self._gateway.delete(self.name, order_id)

    def cancel_all(self, instrument_id: str | None = None) -> None:
        """Cancel this strategy's resting orders (on one instrument, or everywhere)."""
        self._gateway.delete_all(self.name, instrument_id)

    def round_price(self, instrument_id: str, price: float, side: str) -> float:
        """Snap to the tick grid: bids round down, asks round up."""
        return round_to_tick(price, self.market.tick_size(instrument_id), side)
