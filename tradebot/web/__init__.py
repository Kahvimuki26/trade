"""Local web dashboard:  ``tradebot web``  ->  http://127.0.0.1:8765

Security:
  * Binds to 127.0.0.1 (only your computer) by default.
  * Set TRADEBOT_PASSWORD to require a login (HTTP basic auth, any username).
  * Requests that arrive through Cloudflare (Tunnel) are refused unless a
    password is set. See docs/CLOUDFLARE.md for putting it on your domain.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import math
import os
import shutil
import threading
import time
from collections import deque
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from ..backtest import run_backtest
from ..config import InstanceConfig, load_config
from ..data import get_provider
from ..paper import NY, Runner, market_is_open
from ..strategies import get_strategy, list_strategies, strategy_class

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("tradebot")


# --------------------------------------------------------------------- helpers
def clean(x: Any) -> Any:
    """Make numpy / NaN / inf JSON-safe."""
    if isinstance(x, dict):
        return {k: clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if isinstance(x, (np.floating, float)):
        x = float(x)
        return None if math.isnan(x) or math.isinf(x) else x
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, (pd.Timestamp, date)):
        return x.isoformat()
    return x


def downsample(points: list, n: int = 1200) -> list:
    if len(points) <= n:
        return points
    step = len(points) / n
    out = [points[int(i * step)] for i in range(n)]
    out[-1] = points[-1]
    return out


class LogBuffer(logging.Handler):
    def __init__(self, size: int = 400):
        super().__init__()
        self.lines: deque[dict] = deque(maxlen=size)
        self.counter = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.counter += 1
        self.lines.append({"id": self.counter, "time": time.strftime("%H:%M:%S", time.localtime(record.created)),
                           "level": record.levelname, "msg": record.getMessage()})


class DataCache:
    """Caches downloaded bars for an hour so repeated backtests are instant."""

    def __init__(self, ttl: float = 3600):
        self.ttl, self._d, self._lock = ttl, {}, threading.Lock()

    def get(self, source: str, symbol: str, start, end, interval: str, csv_dir: str = "data"):
        key = (source, symbol, str(start), str(end), interval)
        with self._lock:
            hit = self._d.get(key)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
        kwargs = {"directory": csv_dir} if source == "csv" else {}
        df = get_provider(source, **kwargs).get_bars(symbol, start=start, end=end, interval=interval)
        with self._lock:
            self._d[key] = (time.time(), df)
        return df


# ------------------------------------------------------------------ config I/O
class ConfigStore:
    """Edits configs/instances.yaml while keeping it valid (backs up the original once)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self):
        return load_config(self.path)

    def raw(self) -> dict:
        return yaml.safe_load(self.path.read_text()) or {}

    def text(self) -> str:
        return self.path.read_text()

    def _write_text(self, text: str) -> None:
        tmp = self.path.with_suffix(".check.yaml")
        tmp.write_text(text)
        try:
            load_config(tmp)  # validate before replacing
        finally:
            tmp.unlink(missing_ok=True)
        backup = self.path.with_suffix(".yaml.bak")
        if not backup.exists():
            shutil.copy(self.path, backup)
        self.path.write_text(text)

    def write_text(self, text: str) -> None:
        with self._lock:
            self._write_text(text)

    def update(self, fn) -> None:
        with self._lock:
            raw = self.raw()
            raw.setdefault("instances", [])
            fn(raw)
            text = ("# Edited from the tradebot web dashboard. Original kept in instances.yaml.bak\n"
                    + yaml.safe_dump(raw, sort_keys=False, default_flow_style=None, allow_unicode=True))
            self._write_text(text)


# ----------------------------------------------------------------------- app
def create_app(config_path: str | Path = "configs/instances.yaml", runner: Runner | None = None):
    from fastapi import Body, FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, JSONResponse, Response

    store = ConfigStore(config_path)
    runner = runner or Runner(store.load)
    logs = LogBuffer()
    logs.setLevel(logging.INFO)
    logging.getLogger("tradebot").addHandler(logs)
    logging.getLogger("tradebot").setLevel(logging.INFO)
    cache = DataCache()
    password = os.environ.get("TRADEBOT_PASSWORD", "")

    app = FastAPI(title="tradebot", docs_url=None, redoc_url=None)
    app.state.runner, app.state.store = runner, store

    @app.middleware("http")
    async def auth(request: Request, call_next):
        via_cloudflare = "cf-connecting-ip" in request.headers or "cf-ray" in request.headers
        if not password:
            if via_cloudflare:
                return Response("Set TRADEBOT_PASSWORD before exposing tradebot to the internet "
                                "(see docs/CLOUDFLARE.md).", status_code=403)
            return await call_next(request)
        header = request.headers.get("authorization", "")
        ok = False
        if header.lower().startswith("basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode().partition(":")
                ok = hmac.compare_digest(given.encode(), password.encode())
            except Exception:
                ok = False
        if not ok:
            return Response("Login required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="tradebot"'})
        return await call_next(request)

    async def errors(request: Request, exc: Exception):
        msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        return JSONResponse({"error": str(msg)}, status_code=400)

    for exc_type in (ValueError, KeyError, RuntimeError, FileNotFoundError, ImportError, yaml.YAMLError):
        app.add_exception_handler(exc_type, errors)

    def bad(msg: str):
        raise HTTPException(status_code=400, detail=msg)

    # ------------------------------------------------------------- pages
    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    # ------------------------------------------------------------- strategies
    @app.get("/api/strategies")
    def strategies():
        return [clean({
            "name": s.name, "display_name": s.display_name or s.name, "description": s.description,
            "source": s.source, "explainer": s.explainer, "param_help": s.param_help,
            "interval": s.interval, "recommended": s.recommended,
            "params": s.default_params, "notes": s.notes,
        }) for s in sorted(list_strategies(), key=lambda c: (c.name != "buy_and_hold",
                                                              not c.source.startswith("classic"),
                                                              c.display_name or c.name))]

    # ------------------------------------------------------------- overview
    def summary(inst: InstanceConfig, state_dir: str) -> dict:
        path = Path(state_dir) / f"{inst.name}.json"
        row = {**inst.__dict__, "started": False}
        if not path.exists():
            return row
        s = json.loads(path.read_text())
        hist = s.get("equity_history") or []
        start = s.get("starting_cash") or inst.starting_cash
        eq = hist[-1][1] if hist else start
        trades = s.get("trades", s.get("portfolio", {}).get("trades", []))
        closed = [t["realized_pnl"] for t in trades if t.get("realized_pnl") is not None]
        row.update({
            "started": True, "equity": eq, "start_equity": start, "return": eq / start - 1,
            "positions": s.get("positions", s.get("portfolio", {}).get("positions", {})),
            "orders": len(trades), "round_trips": len(closed),
            "win_rate": (sum(p > 0 for p in closed) / len(closed)) if closed else None,
            "last_update": hist[-1][0] if hist else None,
            "last_signals": s.get("last_signals", {}), "last_prices": s.get("last_prices", {}),
            "spark": [h[1] for h in downsample(hist, 60)],
        })
        return row

    @app.get("/api/overview")
    def overview():
        cfg = store.load()
        now = pd.Timestamp.now(tz=NY)
        return clean({
            "market_open": market_is_open(now.to_pydatetime()),
            "ny_time": now.strftime("%a %H:%M"),
            "runner": {"running": runner.running, "last_tick": runner.last_tick,
                       "last_error": runner.last_error, "poll_seconds": cfg.poll_seconds},
            "password": bool(password),
            "instances": [summary(i, cfg.state_dir) for i in cfg.instances],
        })

    @app.get("/api/instances/{name}")
    def instance(name: str):
        cfg = store.load()
        inst = cfg.get(name)
        row = summary(inst, cfg.state_dir)
        path = Path(cfg.state_dir) / f"{name}.json"
        if path.exists():
            s = json.loads(path.read_text())
            row["equity_history"] = downsample(s.get("equity_history", []))
            row["trades"] = list(reversed(s.get("trades", s.get("portfolio", {}).get("trades", []))))[:500]
        return clean(row)

    @app.get("/api/history")
    def history():
        """Return-% curves of every started instance, for the overview chart."""
        cfg = store.load()
        out = []
        for inst in cfg.instances:
            path = Path(cfg.state_dir) / f"{inst.name}.json"
            if not path.exists():
                continue
            s = json.loads(path.read_text())
            hist = s.get("equity_history") or []
            if not hist:
                continue
            base = s.get("starting_cash") or hist[0][1]
            out.append({"name": inst.name, "points": [[t, v / base - 1] for t, v in downsample(hist, 400)]})
        return out

    # ------------------------------------------------------------- editing
    @app.post("/api/instances")
    def save_instance(body: dict = Body(...)):
        name = (body.get("name") or "").strip()
        original = body.pop("original_name", None)  # set when editing an existing instance
        if not name or not body.get("strategy"):
            bad("Name and strategy are required")
        cls = strategy_class(body["strategy"])
        params = {k: v for k, v in (body.get("params") or {}).items()
                  if k in cls.default_params and v != cls.default_params[k]}
        item = {"name": name, "strategy": body["strategy"]}
        if params:
            item["params"] = params
        syms = body.get("symbols")
        if isinstance(syms, str):
            syms = [x.strip().upper() for x in syms.replace(" ", ",").split(",") if x.strip()]
        if syms:
            item["symbols"] = syms
        for key in ("broker", "alpaca_account", "data_source", "interval", "starting_cash", "max_leverage",
                    "enabled"):
            if body.get(key) not in (None, ""):
                item[key] = body[key]

        def apply(raw):
            lst = raw["instances"]
            names = [i.get("name") for i in lst]
            if name in names and name != original:
                bad(f"An instance called '{name}' already exists")
            if original in names:
                lst[names.index(original)] = item
            else:
                lst.append(item)

        store.update(apply)
        return {"ok": True}

    @app.post("/api/instances/{name}/enabled")
    def set_enabled(name: str, body: dict = Body(...)):
        def apply(raw):
            for i in raw["instances"]:
                if i.get("name") == name:
                    i["enabled"] = bool(body.get("enabled"))
                    return
            bad(f"No instance '{name}'")

        store.update(apply)
        return {"ok": True}

    @app.delete("/api/instances/{name}")
    def delete_instance(name: str):
        store.update(lambda raw: raw.__setitem__(
            "instances", [i for i in raw["instances"] if i.get("name") != name]))
        return {"ok": True}

    @app.post("/api/instances/{name}/reset")
    def reset_instance(name: str):
        cfg = store.load()
        path = Path(cfg.state_dir) / f"{name}.json"
        if path.exists():
            shutil.move(path, path.with_name(f"{name}.{time.strftime('%Y%m%d-%H%M%S')}.bak.json"))
        return {"ok": True}

    @app.get("/api/config")
    def get_config():
        return {"text": store.text()}

    @app.put("/api/config")
    def put_config(body: dict = Body(...)):
        store.write_text(body.get("text", ""))
        return {"ok": True}

    # ------------------------------------------------------------- runner
    @app.post("/api/runner/start")
    def start():
        runner.start()
        return {"running": True}

    @app.post("/api/runner/stop")
    def stop():
        threading.Thread(target=runner.stop, daemon=True).start()
        return {"running": False}

    @app.post("/api/runner/tick")
    def tick(body: dict = Body(default={})):
        n = runner.tick(force=bool(body.get("force")))
        return {"updated": n}

    # ------------------------------------------------------------- updates
    update_cache: dict = {"t": 0.0, "info": None}

    @app.get("/api/update")
    def update_check(fresh: bool = False):
        from .. import updater

        if fresh or not update_cache["info"] or time.time() - update_cache["t"] > 900:
            update_cache["info"] = updater.check()
            update_cache["t"] = time.time()
        info = dict(update_cache["info"])
        info["can_restart"] = os.environ.get("TRADEBOT_LAUNCHER") == "1"
        return info

    @app.post("/api/update")
    def update_apply():
        from .. import updater
        from ..launch import RESTART_CODE, RESUME_FILE

        result = updater.update()
        update_cache["info"] = None
        if not result.get("updated"):
            return {"updated": False, "message": result.get("reason")}
        restart = os.environ.get("TRADEBOT_LAUNCHER") == "1"
        log.info("Updated to version %s (%d files)", result["version"][:7],
                 len(result["changed"]) + len(result["added"]))
        if restart:
            if runner.running:
                RESUME_FILE.write_text("1")

            def bye():
                time.sleep(1.0)
                runner.stop()
                os._exit(RESTART_CODE)

            threading.Thread(target=bye, daemon=True).start()
        return {"updated": True, "restarting": restart, "version": result["version"],
                "message": "Updated. Restarting..." if restart else
                "Updated. Close the tradebot window and start it again to use the new version."}

    @app.get("/api/logs")
    def get_logs(after: int = 0):
        return [line for line in logs.lines if line["id"] > after]

    # ------------------------------------------------------------- backtests
    def load_bars(body: dict, symbols: list[str], interval: str) -> dict:
        source = body.get("source") or "yfinance"
        return {s: cache.get(source, s, body.get("start") or "2015-01-01", body.get("end") or None,
                             interval, body.get("csv_dir") or "data") for s in symbols}

    def symbols_of(body: dict, cls=None) -> list[str]:
        syms = body.get("symbols") or ((cls.recommended.get("symbols") or ["SPY"]) if cls else None) or []
        if isinstance(syms, str):
            syms = [x.strip().upper() for x in syms.replace(" ", ",").split(",") if x.strip()]
        if not syms:
            bad("Enter at least one symbol, e.g. SPY")
        return [s.upper() for s in syms]

    def bt_kwargs(body: dict, cls) -> dict:
        lev = body.get("max_leverage")
        return dict(cash=float(body.get("cash") or 10_000), commission=float(body.get("commission") or 0),
                    slippage_bps=float(body.get("slippage_bps") if body.get("slippage_bps") not in (None, "") else 1),
                    max_leverage=float(lev) if lev not in (None, "") else cls.recommended.get("max_leverage", 1.0))

    @app.post("/api/backtest")
    def backtest(body: dict = Body(...)):
        cls = strategy_class(body["strategy"])
        params = {k: v for k, v in (body.get("params") or {}).items() if k in cls.default_params}
        strategy = get_strategy(cls.name, **params)
        interval = body.get("interval") or cls.interval or "1d"
        syms = symbols_of(body, cls)
        data = load_bars(body, syms, interval)
        res = run_backtest(strategy, data, **bt_kwargs(body, cls))
        eq = res.equity
        start_cash = float(eq.iloc[0])
        # buy & hold curve for comparison (equal weight, same dates)
        closes = pd.concat({s: d["close"] for s, d in data.items()}, axis=1).reindex(eq.index).ffill()
        bench = (closes / closes.bfill().iloc[0]).mean(axis=1) * start_cash
        pts = downsample(list(zip(eq.index, eq.to_numpy(), bench.to_numpy())))
        trades = res.trades_frame().iloc[::-1].head(500)
        return clean({
            "title": f"{strategy!r} on {', '.join(syms)}",
            "interval": interval, "symbols": syms,
            "from": str(eq.index[0]), "to": str(eq.index[-1]),
            "metrics": res.metrics,
            "equity": [[str(t), e, b] for t, e, b in pts],
            "trades": trades.to_dict(orient="records"),
        })

    @app.post("/api/compare")
    def compare(body: dict = Body(...)):
        interval = body.get("interval") or "1d"
        names = body.get("strategies") or [s.name for s in list_strategies()]
        runnable = [n for n in names if strategy_class(n).interval in (None, interval)]
        skipped = [n for n in names if n not in runnable]
        data = load_bars(body, symbols_of(body), interval)
        rows = []
        for n in runnable:
            res = run_backtest(get_strategy(n), data, **bt_kwargs(body, strategy_class(n)))
            eq = res.equity
            sampled = eq.iloc[:: max(1, len(eq) // 300)]
            rows.append({"strategy": n, **res.metrics,
                         "curve": [[str(t), v / eq.iloc[0] - 1] for t, v in sampled.items()]})
        rows.sort(key=lambda r: -1e9 if r["sharpe"] is None or (isinstance(r["sharpe"], float) and math.isnan(r["sharpe"])) else r["sharpe"], reverse=True)
        return clean({"rows": rows, "skipped": skipped})

    return app


DEFAULT_PORT = 8765


def port_free(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def pick_port(host: str, wanted: int, tries: int = 20) -> int:
    for port in range(wanted, wanted + tries):
        if port_free(host, port):
            return port
    raise RuntimeError(f"Ports {wanted}-{wanted + tries - 1} are all in use. Set TRADEBOT_PORT in .env "
                       "to a free port.")


def serve(config_path: str = "configs/instances.yaml", host: str = "127.0.0.1", port: int | None = None,
          open_browser: bool = False, autostart: bool = False) -> None:
    import uvicorn

    if host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("TRADEBOT_PASSWORD"):
        raise RuntimeError("Refusing to listen on a public address without a password. "
                           "Set TRADEBOT_PASSWORD or use the default host 127.0.0.1.")
    if not Path(config_path).exists():
        raise FileNotFoundError(f"Config not found: {config_path} (run from the project folder)")
    wanted = port or int(os.environ.get("TRADEBOT_PORT") or DEFAULT_PORT)
    port = pick_port(host, wanted)
    if port != wanted:
        print(f"\n  Port {wanted} is busy (another program uses it), so using {port} instead."
              f"\n  To always use a fixed port, add TRADEBOT_PORT=<number> to .env")
    app = create_app(config_path)
    if autostart:
        app.state.runner.start()
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    print(f"\n  tradebot dashboard: {url}\n  (Ctrl+C to stop)\n")
    if open_browser:
        import webbrowser

        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
