"""Every tunable number in one place.

Values marked CONFIRM are placeholders until the event tutorial states the real limits.
Change the defaults here, or pass a customised Settings(...) to bot.run().
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    # Risk: checked before any order leaves the bot, account-wide (all strategies combined).
    position_limit: int = 100                    # CONFIRM: max |position| per instrument
    position_limits: dict[str, int] = field(default_factory=dict)  # per-instrument overrides
    max_order_volume: int = 50                   # largest single order the bot will send
    max_open_orders_per_instrument: int = 20     # stops a buggy loop from piling up orders
    max_messages_per_second: int = 20            # CONFIRM: inserts + amends + deletes, all strategies

    # Kill switch: if PnL stays below kill_pnl for kill_confirm_seconds, cancel everything,
    # flatten and stop. None disables it.
    kill_pnl: float | None = None
    kill_confirm_seconds: float = 3.0

    # Main loop
    tick_interval: float = 0.1                   # seconds between strategy ticks
    status_interval: float = 5.0                 # seconds between status lines / pnl.csv rows
    reconnect_delay_max: float = 10.0            # cap for the exponential reconnect back-off

    # Lifecycle
    cancel_orders_on_start: bool = True          # clear orders left behind by a previous run
    flatten_on_stop: bool = False                # trade positions back to zero on a normal shutdown

    # Market data kept in memory, and recorded to exports/ for offline research
    trade_history: int = 1000                    # public trades kept per instrument
    post_history: int = 500                      # social media posts kept
    record_market_data: bool = True              # books.csv, trades.csv, posts.csv

    def limit_for(self, instrument_id: str) -> int:
        return self.position_limits.get(instrument_id, self.position_limit)


def exchange_kwargs_from_env() -> dict[str, object]:
    """Connection arguments for optibook's Exchange(), from OPTIBOOK_* environment variables.

    Unset variables are left out so Exchange() uses its own defaults; the hosted environment is
    normally pre-configured and needs none of them. Never hard-code credentials in the repo.
    """
    names = {
        "host": "OPTIBOOK_HOST",
        "info_port": "OPTIBOOK_INFO_PORT",
        "exec_port": "OPTIBOOK_EXEC_PORT",
        "username": "OPTIBOOK_USERNAME",
        "password": "OPTIBOOK_PASSWORD",
    }
    kwargs: dict[str, object] = {}
    for arg, var in names.items():
        value = os.environ.get(var)
        if value:
            kwargs[arg] = int(value) if arg.endswith("_port") else value
    return kwargs
