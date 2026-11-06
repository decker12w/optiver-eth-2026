"""Command line entry point: python -m bot --help"""
from __future__ import annotations

import argparse
import signal
import sys

from bot import loader
from bot.engine import run
from bot.reporting import setup_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bot",
        description="Run strategies from the strategies/ folder on Optibook (or the offline mock).",
        epilog="examples:\n"
               "  python -m bot --list\n"
               "  python -m bot -s example_quoter --mock --duration 30\n"
               "  python -m bot -s example_signals,example_quoter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-s", "--strategy", action="append", default=[], metavar="NAME[,NAME]",
                        help="strategy to run; repeat it or comma-separate names to combine strategies")
    parser.add_argument("--list", action="store_true", help="list available strategies and exit")
    parser.add_argument("--mock", action="store_true", help="trade against the offline mock exchange")
    parser.add_argument("--seed", type=int, help="random seed for the mock exchange")
    parser.add_argument("--duration", type=float, metavar="SECONDS", help="stop after this many seconds")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging (every order)")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    if args.list:
        for name in loader.available():
            print(f"{name:24} {loader.describe(name)}")
        return 0

    names = [n.strip() for arg in args.strategy for n in arg.split(",") if n.strip()]
    if not names:
        parser.error("choose at least one strategy with -s NAME (see --list)")

    # `kill <pid>` takes the same clean path as Ctrl+C: cancel orders, report, disconnect.
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    try:
        run(names, mock=args.mock, seed=args.seed, duration=args.duration)
    except (loader.StrategyLoadError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


def _raise_keyboard_interrupt(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    sys.exit(main())
