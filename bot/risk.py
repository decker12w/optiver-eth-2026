"""Pre-trade risk checks and the kill switch.

Checks are account-wide: position limits are shared by every strategy, so they have to be
checked against the combined state, not per strategy.
"""
from __future__ import annotations

from bot.account import Account
from bot.models import ASK, BID, LIMIT, Order
from bot.settings import Settings


class RiskManager:
    def __init__(self, settings: Settings, account: Account):
        self._settings = settings
        self._account = account

    def room(self, instrument_id: str, side: str) -> int:
        """Lots we can still add on `side` without risking the position limit.

        Worst case: the current position plus every resting order and every IOC sent this tick
        on the same side, as if all of them filled.
        """
        limit = self._settings.limit_for(instrument_id)
        position = self._account.position(instrument_id)
        headroom = limit - position if side == BID else limit + position
        pending = (self._account.resting_volume(instrument_id, side)
                   + self._account.ioc_volume_this_tick(instrument_id, side))
        return max(0, headroom - pending)

    def check(self, instrument_id: str, side: str, price: float, volume: int,
              order_type: str) -> tuple[int, str | None]:
        """Largest safe volume (<= `volume`) for this order, plus the reason when it is 0.

        Too-large orders are clipped rather than refused, so a strategy near its limit still
        trades whatever fits.
        """
        if order_type == LIMIT:
            open_orders = len(self._account.orders(instrument_id))
            if open_orders >= self._settings.max_open_orders_per_instrument:
                return 0, "too many open orders"
        crossed = self._own_order_crossed(instrument_id, side, price)
        if crossed is not None:
            return 0, "would trade with own order"
        allowed = min(volume, self._settings.max_order_volume, self.room(instrument_id, side))
        if allowed <= 0:
            return 0, "position limit"
        return allowed, None

    def _own_order_crossed(self, instrument_id: str, side: str, price: float) -> Order | None:
        """A resting order of ours on the other side that this order would trade against."""
        for order in self._account.orders(instrument_id):
            if side == BID and order.side == ASK and price >= order.price:
                return order
            if side == ASK and order.side == BID and price <= order.price:
                return order
        return None


class KillSwitch:
    """Trips when PnL stays below `threshold` for `confirm_seconds` in a row.

    The confirmation period stops one bad mark (e.g. a momentarily empty book) from shutting the
    bot down. A threshold of None disables the switch.
    """

    def __init__(self, threshold: float | None, confirm_seconds: float):
        self.threshold = threshold
        self._confirm_seconds = confirm_seconds
        self._below_since: float | None = None

    def update(self, pnl: float | None, now: float) -> bool:
        if self.threshold is None or pnl is None:
            return False
        if pnl >= self.threshold:
            self._below_since = None
            return False
        if self._below_since is None:
            self._below_since = now
        return now - self._below_since >= self._confirm_seconds
