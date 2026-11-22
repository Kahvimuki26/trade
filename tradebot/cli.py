"""Command line:  tradebot web | test | update   (most people just use start.bat / start.command)"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing env vars win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def cmd_web(args) -> None:
    from .web import serve

    serve(host=args.host, port=args.port, open_browser=args.open, autostart=args.start)


def cmd_test(args) -> None:
    from .data import Binance
    from .sim import simulate

    r = simulate(Binance(), [c.upper() for c in args.coins], days=args.days, risk=args.risk, cash=args.cash)
    print(f"\n{args.days} days, {args.risk}, {', '.join(r['coins'])}: ${args.cash:,.2f} -> ${r['end']:,.2f} "
          f"({r['return'] * 100:+.1f}%)")
    print(f"just holding the coins: {r['hold_return'] * 100:+.1f}%   worst drop: {r['max_drawdown'] * 100:.1f}%   "
          f"trades: {r['trades']}   stop-losses: {r['stops']}")
    print("playbooks used: " + ", ".join(f"{k} {v * 100:.0f}%" for k, v in r["playbook_share"].items()))


def cmd_update(args) -> None:
    from . import updater

    updater.main(["--check"] if args.check else (["--force"] if args.force else []))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tradebot", description="Crypto autopilot (paper trading).")
    sub = p.add_subparsers(dest="command", required=True)

    w = sub.add_parser("web", help="Open the dashboard")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=None, help="Default 8765 (or TRADEBOT_PORT)")
    w.add_argument("--open", action="store_true", help="Open the browser")
    w.add_argument("--start", action="store_true", help="Start the bot right away")
    w.set_defaults(func=cmd_web)

    t = sub.add_parser("test", help="Replay the autopilot on past prices")
    t.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SOL"])
    t.add_argument("--days", type=int, default=90)
    t.add_argument("--risk", default="normal", choices=["safe", "normal", "aggressive"])
    t.add_argument("--cash", type=float, default=100)
    t.set_defaults(func=cmd_test)

    u = sub.add_parser("update", help="Download the latest version")
    u.add_argument("--check", action="store_true")
    u.add_argument("--force", action="store_true")
    u.set_defaults(func=cmd_update)
    return p


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nStopped.")
    except (ValueError, RuntimeError, ConnectionError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
