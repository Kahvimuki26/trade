"""Command line interface:  ``tradebot <command> --help``"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import shutil
import sys
from datetime import date
from pathlib import Path

import yaml

from .backtest import run_backtest
from .config import load_config
from .data import get_provider
from .metrics import fmt
from .strategies import get_strategy, list_strategies, strategy_class

DEFAULT_CONFIG = "configs/instances.yaml"


def table(rows: list[dict], columns: list[str] | None = None) -> str:
    if not rows:
        return "(nothing)"
    columns = columns or list(rows[0])
    cells = [[str(r.get(c, "")) for c in columns] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(columns)]
    numeric = [all(re.fullmatch(r"[-+]?[\d,.]+%?|-|inf", row[i]) for row in cells) for i in range(len(columns))]
    line = lambda vals: "  ".join(v.rjust(w) if num else v.ljust(w)
                                  for v, w, num in zip(vals, widths, numeric))
    return "\n".join([line(columns), line(["-" * w for w in widths]), *(line(r) for r in cells)])


def parse_params(pairs: list[str]) -> dict:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--param must look like key=value, got {p!r}")
        k, v = p.split("=", 1)
        out[k.strip()] = yaml.safe_load(v)
    return out


def load_data(args, symbols: list[str], interval: str) -> dict:
    kwargs = {"directory": args.csv_dir} if args.source == "csv" else {}
    provider = get_provider(args.source, **kwargs)
    return {s.upper(): provider.get_bars(s.upper(), start=args.start, end=args.end, interval=interval)
            for s in symbols}


def bt_kwargs(args, cls=None) -> dict:
    lev = args.max_leverage
    if lev is None:
        lev = (cls.recommended.get("max_leverage", 1.0) if cls else 1.0)
    return dict(cash=args.cash, commission=args.commission, slippage_bps=args.slippage_bps,
                fractional=not args.whole_shares, max_leverage=lev)


# ------------------------------------------------------------------ commands
def cmd_list(args) -> None:
    rows = [{"name": s.name, "bars": s.interval or "any", "description": s.description}
            for s in list_strategies()]
    print(table(rows))
    print("\nMore about one:  tradebot info <name>")


def cmd_info(args) -> None:
    cls = strategy_class(args.strategy)
    print(f"\n{cls.name}\n{'=' * len(cls.name)}")
    print(cls.description)
    print(f"\nsource:      {cls.source or '-'}")
    print(f"bar size:    {cls.interval or 'any (default 1d)'}")
    if cls.recommended:
        print("uses:        " + ", ".join(f"{k}={v}" for k, v in cls.recommended.items()))
    print("parameters:  " + (", ".join(f"{k}={v!r}" for k, v in cls.default_params.items()) or "none"))
    if cls.notes:
        print("\nnotes:")
        for line in cls.notes.splitlines():
            print(f"  {line}")
    sym = " ".join(cls.recommended.get("symbols", ["SPY"]))
    src = " --source alpaca" if cls.interval and cls.interval.endswith("m") else ""
    print(f"\ntry:  tradebot backtest {cls.name} {sym}{src}\n")


def cmd_backtest(args) -> None:
    strategy = get_strategy(args.strategy, **parse_params(args.param))
    cls = type(strategy)
    interval = args.interval or cls.interval or "1d"
    symbols = args.symbols or cls.recommended.get("symbols")
    if not symbols:
        raise SystemExit("Give at least one symbol, e.g.  tradebot backtest sma_cross SPY")
    if cls.interval and interval != cls.interval:
        print(f"warning: {cls.name} is designed for {cls.interval} bars, running on {interval}")
    data = load_data(args, symbols, interval)
    res = run_backtest(strategy, data, **bt_kwargs(args, cls))
    first = min(df.index[0] for df in data.values()).date()
    last = max(df.index[-1] for df in data.values()).date()
    print(f"\n{strategy!r} on {', '.join(data)}  |  {first} -> {last}  |  {interval} bars  |  "
          f"max leverage {bt_kwargs(args, cls)['max_leverage']}\n")
    for k, v in res.metrics.items():
        print(f"  {k:<18} {fmt(k, v)}")
    if args.trades:
        print("\nTrades:")
        print(res.trades_frame().to_string(index=False))
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        res.equity.to_csv(out / "equity.csv")
        res.trades_frame().to_csv(out / "trades.csv", index=False)
        print(f"\nWrote {out}/equity.csv and {out}/trades.csv")


def cmd_compare(args) -> None:
    interval = args.interval or "1d"
    names = args.strategies or [s.name for s in list_strategies()]
    skipped = [n for n in names if strategy_class(n).interval not in (None, interval)]
    names = [n for n in names if n not in skipped]
    data = load_data(args, args.symbols, interval)
    rows = []
    for name in names:
        res = run_backtest(get_strategy(name), data, **bt_kwargs(args, strategy_class(name)))
        m = res.metrics
        rows.append({"strategy": name, **{k: m[k] for k in
                     ("total_return", "cagr", "sharpe", "max_drawdown", "win_rate", "round_trips", "exposure")}})
    key = args.sort  # higher is better for all, incl. max_drawdown (it's negative)
    rows.sort(key=lambda r: -math.inf if (isinstance(r[key], float) and math.isnan(r[key])) else r[key],
              reverse=True)
    first = min(df.index[0] for df in data.values()).date()
    print(f"\n{', '.join(data)}  |  {first} -> {max(df.index[-1] for df in data.values()).date()}"
          f"  |  sorted by {key}\n")
    print(table([{k: fmt(k, v) if k != "strategy" else v for k, v in r.items()} for r in rows]))
    if skipped:
        print(f"\nSkipped (need other bar sizes): "
              + ", ".join(f"{n} ({strategy_class(n).interval})" for n in skipped)
              + f". Try --interval {strategy_class(skipped[0]).interval}")


def cmd_paper(args) -> None:
    from . import paper

    cfg = load_config(args.config)
    paper.run(cfg, names=args.instance, once=args.once, force=args.force)


def cmd_status(args) -> None:
    cfg = load_config(args.config)
    rows = []
    for inst in cfg.instances:
        path = Path(cfg.state_dir) / f"{inst.name}.json"
        if not path.exists():
            rows.append({"instance": inst.name, "strategy": inst.strategy, "broker": inst.broker,
                         "symbols": ",".join(inst.symbols),
                         "equity": "-", "return": "not started", "positions": "", "orders": 0, "last update": ""})
            continue
        s = json.loads(path.read_text())
        hist = s.get("equity_history") or [[None, s["starting_cash"]]]
        eq = hist[-1][1]
        pos = s.get("positions", s.get("portfolio", {}).get("positions", {}))
        ntrades = len(s.get("trades", s.get("portfolio", {}).get("trades", [])))
        rows.append({
            "instance": inst.name,
            "strategy": s["strategy"],
            "broker": s.get("broker", "sim"),
            "symbols": ",".join(s["symbols"]),
            "equity": f"{eq:,.2f}",
            "return": fmt("total_return", eq / s["starting_cash"] - 1),
            "positions": " ".join(f"{k}:{v:.2f}" for k, v in pos.items()) or "flat",
            "orders": ntrades,
            "last update": hist[-1][0] or "",
        })
    print(table(rows))


def cmd_trades(args) -> None:
    cfg = load_config(args.config)
    path = Path(cfg.state_dir) / f"{args.name}.json"
    if not path.exists():
        raise SystemExit(f"No state for '{args.name}' yet")
    s = json.loads(path.read_text())
    trades = s.get("trades", s.get("portfolio", {}).get("trades", []))
    rows = [{"time": t["ts"], "symbol": t["symbol"], "side": "BUY" if t["qty"] > 0 else "SELL",
             "qty": f"{abs(t['qty']):.4f}", "price": f"{t['price']:.2f}",
             "pnl": "" if t["realized_pnl"] is None else f"{t['realized_pnl']:+,.2f}"} for t in trades]
    print(table(rows[-args.last:] if args.last else rows))


def cmd_reset(args) -> None:
    cfg = load_config(args.config)
    cfg.get(args.name)
    path = Path(cfg.state_dir) / f"{args.name}.json"
    if path.exists():
        backup = path.with_name(f"{args.name}.{date.today():%Y%m%d}.bak.json")
        shutil.move(path, backup)
        print(f"Reset '{args.name}'. Old state saved as {backup}")
    else:
        print(f"'{args.name}' has no state. Nothing to reset.")


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tradebot", description="Backtest and paper-trade stock strategies.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List available strategies").set_defaults(func=cmd_list)
    inf = sub.add_parser("info", help="Everything about one strategy: bar size, params, notes")
    inf.add_argument("strategy")
    inf.set_defaults(func=cmd_info)

    def data_args(sp):
        sp.add_argument("--start", default="2015-01-01", help="Start date (default 2015-01-01)")
        sp.add_argument("--end", default=None, help="End date (default today)")
        sp.add_argument("--interval", default=None,
                        help="Bar size: 1d, 1h, 15m, 5m, ... (default: what the strategy needs, else 1d)")
        sp.add_argument("--source", default="yfinance", choices=["yfinance", "alpaca", "csv"])
        sp.add_argument("--csv-dir", default="data", help="Folder with <SYMBOL>.csv for --source csv")
        sp.add_argument("--cash", type=float, default=10_000)
        sp.add_argument("--commission", type=float, default=0.0, help="Flat fee per order")
        sp.add_argument("--slippage-bps", type=float, default=1.0)
        sp.add_argument("--whole-shares", action="store_true", help="Disallow fractional shares")
        sp.add_argument("--max-leverage", type=float, default=None,
                        help="Max exposure / equity (default: what the strategy recommends, else 1)")

    b = sub.add_parser("backtest", help="Backtest one strategy")
    b.add_argument("strategy")
    b.add_argument("symbols", nargs="*", help="Default: the strategy's recommended symbols")
    b.add_argument("-p", "--param", action="append", help="Override a parameter: -p fast=10 -p slow=30")
    b.add_argument("--trades", action="store_true", help="Print every trade")
    b.add_argument("--out", help="Folder to write equity.csv and trades.csv")
    data_args(b)
    b.set_defaults(func=cmd_backtest)

    c = sub.add_parser("compare", help="Backtest every strategy (or a chosen few) side by side")
    c.add_argument("symbols", nargs="+")
    c.add_argument("-s", "--strategies", nargs="+", help="Only these strategies")
    c.add_argument("--sort", default="sharpe",
                   choices=["total_return", "cagr", "sharpe", "max_drawdown", "win_rate"])
    data_args(c)
    c.set_defaults(func=cmd_compare)

    def cfg_arg(sp):
        sp.add_argument("-c", "--config", default=DEFAULT_CONFIG)

    pp = sub.add_parser("paper", help="Run paper-trading instances from the config")
    cfg_arg(pp)
    pp.add_argument("-i", "--instance", action="append", help="Only run this instance (repeatable)")
    pp.add_argument("--once", action="store_true", help="Run a single tick and exit (for cron)")
    pp.add_argument("--force", action="store_true", help="Ignore market hours / trade window")
    pp.set_defaults(func=cmd_paper)

    st = sub.add_parser("status", help="Show every instance's equity and positions")
    cfg_arg(st)
    st.set_defaults(func=cmd_status)

    tr = sub.add_parser("trades", help="Show an instance's trade log")
    tr.add_argument("name")
    tr.add_argument("-n", "--last", type=int, default=0, help="Only the last N trades")
    cfg_arg(tr)
    tr.set_defaults(func=cmd_trades)

    rs = sub.add_parser("reset", help="Start an instance over (old state is backed up)")
    rs.add_argument("name")
    cfg_arg(rs)
    rs.set_defaults(func=cmd_reset)
    return p


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing env vars win."""
    import os

    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logging.getLogger("yfinance").setLevel(logging.WARNING)
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nStopped.")
    except (KeyError, ValueError, RuntimeError, FileNotFoundError) as e:
        if args.verbose:
            raise
        sys.exit(f"error: {e.args[0] if isinstance(e, KeyError) and e.args else e}")


if __name__ == "__main__":
    main()
