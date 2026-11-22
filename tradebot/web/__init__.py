"""The one-screen dashboard:  ``tradebot web``  ->  http://127.0.0.1:8765

Security: listens on 127.0.0.1 (this computer only). Set TRADEBOT_PASSWORD in .env
to require a login; requests that come in through Cloudflare are refused without one.
"""

from __future__ import annotations

import base64
import hmac
import logging
import os
import threading
import time
from pathlib import Path

from ..autopilot import PLAYBOOKS, RISK
from ..bot import KINDS, Bot
from ..data import COINS

STATIC = Path(__file__).parent / "static"
DEFAULT_PORT = 8765
log = logging.getLogger("tradebot")


def make_bots(market=None) -> dict[str, Bot]:
    """One account per trader, so they can be compared side by side. The real-money one only
    exists when .env has LIVE_TRADING=yes."""
    from ..live import live_config

    bots = {"autopilot": Bot(market=market, path=Path("data/bot.json"), kind="autopilot"),
            "claude": Bot(market=market, path=Path("data/claude.json"), kind="claude")}
    cfg = live_config()
    if cfg:
        bots["live"] = Bot(market=market, path=Path("data/live.json"), live=cfg)
    return bots


def create_app(bots: dict[str, Bot] | Bot | None = None):
    from fastapi import Body, FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse, Response

    if isinstance(bots, Bot):
        bots = {bots.kind: bots}
    bots = bots or make_bots()
    password = os.environ.get("TRADEBOT_PASSWORD", "")
    app = FastAPI(title="tradebot", docs_url=None, redoc_url=None)
    app.state.bots = bots
    app.state.bot = next(iter(bots.values()))

    def pick(name: str | None) -> Bot:
        name = name or next(iter(bots))
        if name not in bots:
            raise ValueError(f"Unknown bot '{name}'")
        return bots[name]

    @app.middleware("http")
    async def auth(request: Request, call_next):
        via_cloudflare = "cf-connecting-ip" in request.headers or "cf-ray" in request.headers
        if not password:
            if via_cloudflare:
                return Response("Set TRADEBOT_PASSWORD in .env before putting tradebot online.", status_code=403)
            return await call_next(request)
        ok = False
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode().partition(":")
                ok = hmac.compare_digest(given.encode(), password.encode())
            except Exception:
                ok = False
        if not ok:
            return Response("Login required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="tradebot"'})
        return await call_next(request)

    async def errors(request: Request, exc: Exception):
        return JSONResponse({"error": str(exc.args[0] if isinstance(exc, KeyError) and exc.args else exc)},
                            status_code=400)

    for exc_type in (ValueError, KeyError, RuntimeError, ConnectionError, FileNotFoundError):
        app.add_exception_handler(exc_type, errors)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    def state(bot: str | None = None):
        b = pick(bot)
        snap = b.snapshot()
        snap["coins_available"] = COINS
        snap["playbooks"] = PLAYBOOKS
        snap["bots"] = {k: {"name": KINDS[k], "balance": x.portfolio.current_equity(x.prices),
                            "start_cash": x.settings["cash"], "running": x.running, "started": x.started}
                        for k, x in bots.items()}
        return snap

    @app.post("/api/start")
    def start(bot: str | None = None, body: dict = Body(default={})):
        b = pick(bot)
        if b.kind == "live" and (body or {}).get("confirm") != "REAL":
            raise ValueError("Real-money trading needs confirmation.")
        if b.kind in ("claude", "live") and not b._ai_configured():
            raise ValueError("Add your Anthropic API key first (ANTHROPIC_API_KEY in the .env file), "
                             "then restart tradebot.")
        b.start()
        return {"running": True}

    @app.post("/api/panic")
    def panic(bot: str | None = None):
        return {"sold": pick(bot).panic()}

    @app.post("/api/stop")
    def stop(bot: str | None = None):
        threading.Thread(target=pick(bot).stop, daemon=True).start()
        return {"running": False}

    @app.post("/api/settings")
    def settings(body: dict = Body(...), bot: str | None = None):
        pick(bot).update_settings(risk=body.get("risk"), coins=body.get("coins"),
                                  ai_every_hours=body.get("ai_every_hours"), ai_news=body.get("ai_news"),
                                  ai_daily_budget=body.get("ai_daily_budget"))
        return {"ok": True}

    @app.post("/api/reset")
    def reset(body: dict = Body(...), bot: str | None = None):
        cash = float(body.get("cash") or 100)
        if not 1 <= cash <= 10_000_000:
            raise ValueError("Pick an amount between $1 and $10,000,000")
        pick(bot).reset(cash=cash, risk=body.get("risk"), coins=body.get("coins"))
        return {"ok": True}

    @app.post("/api/test")
    def test(body: dict = Body(...), bot: str | None = None):
        from ..sim import simulate

        b = pick(bot)
        if b.kind in ("claude", "live"):
            raise ValueError("Claude can't be tested on the past: it may remember what prices did, so the result "
                             "would be fake. It can only be judged live.")
        s = b.settings
        risk = body.get("risk") or s["risk"]
        if risk not in RISK:
            raise ValueError("Unknown risk level")
        return simulate(b.market, body.get("coins") or s["coins"], days=int(body.get("days") or 90),
                        risk=risk, cash=float(body.get("cash") or s["cash"]))

    # ---------------------------------------------------------------- updates
    cache: dict = {"t": 0.0, "info": None}

    @app.get("/api/update")
    def update_check(fresh: bool = False):
        from .. import updater

        if fresh or not cache["info"] or time.time() - cache["t"] > 900:
            cache["info"], cache["t"] = updater.check(), time.time()
        return {**cache["info"], "can_restart": os.environ.get("TRADEBOT_LAUNCHER") == "1"}

    @app.post("/api/update")
    def update_apply():
        from .. import updater
        from ..launch import RESTART_CODE

        result = updater.update()
        cache["info"] = None
        if not result.get("updated"):
            return {"updated": False, "message": result.get("reason")}
        restart = os.environ.get("TRADEBOT_LAUNCHER") == "1"
        if restart:
            def bye():
                time.sleep(1.0)
                for b in bots.values():
                    b.stop(pause=False)  # resumes after the restart if it was running
                    b.save()
                os._exit(RESTART_CODE)

            for b in bots.values():
                b.save()
            threading.Thread(target=bye, daemon=True).start()
        return {"updated": True, "restarting": restart,
                "message": "Updated. Restarting..." if restart else "Updated. Close tradebot and open it again."}

    return app


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
    raise RuntimeError(f"Ports {wanted}-{wanted + tries - 1} are all in use. Set TRADEBOT_PORT in .env.")


def serve(host: str = "127.0.0.1", port: int | None = None, open_browser: bool = False,
          autostart: bool = False) -> None:
    import uvicorn

    if host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("TRADEBOT_PASSWORD"):
        raise RuntimeError("Refusing to listen on a public address without TRADEBOT_PASSWORD.")
    wanted = port or int(os.environ.get("TRADEBOT_PORT") or DEFAULT_PORT)
    port = pick_port(host, wanted)
    if port != wanted:
        print(f"\n  Port {wanted} is busy, so using {port} instead (set TRADEBOT_PORT in .env to fix one).")
    if os.environ.get("TRADEBOT_DEMO"):  # developer demo: made-up prices, separate state files
        from ..data import Synthetic

        m = Synthetic()
        bots = {"autopilot": Bot(market=m, path=Path("data/demo.json")),
                "claude": Bot(market=m, path=Path("data/demo-claude.json"), kind="claude")}
    else:
        bots = make_bots()
    app = create_app(bots)
    for b in bots.values():
        if b.want_running or (autostart and b.kind == "autopilot"):  # was running when closed -> keep going
            if b.kind == "autopilot" or b._ai_configured():
                b.start()
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    print(f"\n  tradebot: {url}\n  Keep this window open. Close it to stop.\n")
    if open_browser:
        import webbrowser

        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
