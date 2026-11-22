"""Claude as the trader.

Every decision, Claude gets a compact picture of each coin (recent price action,
indicators, how the autopilot's playbooks have done), the account, and its own notes
from last time. It answers with buy / sell / hold / stay-out per coin plus reasons.

Hard limits are enforced in code, not left to the model: position size caps from the
risk level, no borrowing, long only, and the stop-loss checked every minute.
"""

from __future__ import annotations

import json
import math
import os

import pandas as pd

from . import indicators as ind
from .autopilot import ACTIVE, COST, PLAYBOOKS, RISK, analyze

MODEL = "claude-opus-5-5"
PRICE_IN, PRICE_OUT = 4.00 / 1e6, 20.00 / 1e6  # $ per token (Claude Opus 5.5)
PRICE_CACHE_READ, PRICE_CACHE_WRITE = 0.20 / 1e6, 5.00 / 1e6
PRICE_SEARCH = 10.00 / 1000  # $ per web search
BETAS = ["server-side-fallback-2026-07-01"]

NEWS_SYSTEM = """You are the news researcher for a crypto trading bot. Search the web for the most recent \
news (last 24-48 hours) that could move the prices of the coins you're given: market-wide crypto news, \
macro events (rates, inflation data, stock market moves), regulation, ETF flows, exchange or protocol \
hacks, big listings or unlocks, and overall market sentiment. Prefer reputable sources and recent dates.

Then write a short briefing (max ~250 words): one line on the overall market mood, then one or two bullet \
points per coin with the news that matters and the date. Say plainly when you found nothing important. \
Don't give trading advice; just report what's happening."""
ACTIONS = ["buy", "sell", "hold", "stay_out"]

SYSTEM = """You are the trader inside a crypto paper-trading app. The user is a beginner who wants to see \
whether an AI can trade well. It is play money on real live prices, so make real decisions as if it mattered, \
but never pretend to know the future.

Each turn you get hourly market data for the coins you may trade, the account, open positions, your own \
notes from your previous decision and, when available, a fresh news briefing. Weigh the news against what \
the prices are actually doing (news is often already priced in). Decide what to do with each coin right now.

Rules (also enforced in code, so don't try to work around them):
- Spot only: you can buy and hold coins or be in cash. No shorting, no leverage.
- Position size is a percentage of total account value, capped per coin by the risk level, and the total \
can't exceed 100%.
- Every position has an automatic stop-loss at the risk level's percentage below the entry price, checked every minute.
- Each buy or sell costs about 0.12% in fees, so trading back and forth for tiny moves loses money.
- Cash is a valid position. Only buy when you see a real reason; it's fine to do nothing.

Actions per coin:
- buy: open a position (only if you don't hold it). Give size_pct.
- sell: close the position you hold.
- hold: keep the position you hold.
- stay_out: don't hold it.

Write reasons in plain, short English a beginner understands (no jargon without a 3-word explanation). \
Be honest about uncertainty. In note_to_self, write what you want to remember next hour (your plan, \
what would change your mind)."""


def schema(coins: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "market_view": {"type": "string", "description": "1-2 sentences on the overall market right now."},
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "coin": {"type": "string", "enum": coins},
                        "action": {"type": "string", "enum": ACTIONS},
                        "size_pct": {"type": "number", "description": "For buy: % of account value. Else 0."},
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["coin", "action", "size_pct", "confidence", "reason"],
                    "additionalProperties": False,
                },
            },
            "note_to_self": {"type": "string"},
        },
        "required": ["market_view", "decisions", "note_to_self"],
        "additionalProperties": False,
    }


def usage_cost(usage) -> tuple[float, int]:
    """Dollar cost of one API response, and the number of web searches it ran."""
    g = lambda name: getattr(usage, name, 0) or 0  # noqa: E731
    stu = getattr(usage, "server_tool_use", None)
    searches = (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
    cost = (g("input_tokens") * PRICE_IN + g("output_tokens") * PRICE_OUT
            + g("cache_read_input_tokens") * PRICE_CACHE_READ
            + g("cache_creation_input_tokens") * PRICE_CACHE_WRITE + searches * PRICE_SEARCH)
    return cost, searches


def _r(x, d=2):
    try:
        x = float(x)
        return round(x, d) if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def coin_brief(coin: str, df: pd.DataFrame, lookback_days: int) -> dict:
    """Compact numbers about one coin, from hourly candles (closed ones only)."""
    close = df["close"]
    last = float(close.iloc[-1])
    an = analyze(df, lookback_days).iloc[-1]
    ema20, ema50, ema200 = (float(ind.ema(close, n).iloc[-1]) for n in (20, 50, 200))
    week = df.iloc[-168:]
    hourly_ret = close.pct_change().iloc[-168:]
    return {
        "price": _r(last, 6),
        "change_pct": {k: _r((last / float(close.iloc[-h - 1]) - 1) * 100) for k, h in
                       (("1h", 1), ("4h", 4), ("24h", 24), ("7d", 168)) if len(close) > h},
        "rsi_14h": _r(ind.rsi(close, 14).iloc[-1], 1),
        "price_vs_ema_pct": {"ema20h": _r((last / ema20 - 1) * 100), "ema50h": _r((last / ema50 - 1) * 100),
                             "ema200h": _r((last / ema200 - 1) * 100)},
        "week_range": {"high": _r(week["high"].max(), 6), "low": _r(week["low"].min(), 6)},
        "volatility_daily_pct": _r(hourly_ret.std() * math.sqrt(24) * 100),
        "trendiness_0to1": _r(an.get("efficiency"), 2),
        "last_12h_closes": [_r(x, 6) for x in close.iloc[-12:]],
        "autopilot_playbooks_last_%dd_pct" % lookback_days: {
            PLAYBOOKS[p]["label"]: _r((an.get(f"perf_{p}") or 0) * 100) for p in ACTIVE},
    }


class ClaudeTrader:
    def __init__(self, client=None):
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    @staticmethod
    def configured() -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    def news(self, coins: list[str]) -> tuple[str, float, int]:
        """Web-search briefing on what's moving these coins. Returns (text, cost, searches)."""
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 4}]
        messages = [{"role": "user", "content": f"Coins: {', '.join(coins)}. Today is "
                                                f"{pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M} UTC. "
                                                "Research and write the briefing."}]
        cost, searches, response = 0.0, 0, None
        for _ in range(3):  # resume if the server-side search loop pauses
            response = self.client.beta.messages.create(
                model=MODEL, max_tokens=16000, betas=BETAS, fallbacks="default", system=NEWS_SYSTEM,
                output_config={"effort": "medium"}, tools=tools, messages=messages)
            c, n = usage_cost(response.usage)
            cost, searches = cost + c, searches + n
            if response.stop_reason != "pause_turn":
                break
            messages = messages[:1] + [{"role": "assistant", "content": response.content}]
        if response is None or response.stop_reason == "refusal":
            return "(news unavailable this time)", cost, searches
        text = "\n".join(b.text for b in response.content if getattr(b, "type", "") == "text").strip()
        return text or "(no news found)", cost, searches

    def build_prompt(self, frames: dict, account: dict, risk: str, memory: dict, news: str | None = None,
                     live: bool = False) -> str:
        cfg = RISK[risk]
        briefs = {c: coin_brief(c, df, cfg["lookback_days"]) for c, df in frames.items() if len(df) >= 210}
        payload = {
            "time_utc": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M"),
            "risk_level": {"name": cfg["label"], "max_coins_at_once": cfg["max_coins"],
                           "max_size_pct_per_coin": int(cfg["per_coin"] * 100),
                           "stop_loss_pct": int(cfg["stop"] * 100)},
            "fees_pct_per_trade": round(COST * 100, 2),
            "account": account,
            "coins": briefs,
            "your_note_from_last_time": memory.get("note") or "(first decision)",
            "your_recent_decisions": memory.get("recent", [])[-6:],
        }
        if news is not None:
            payload["news_briefing"] = news
        head = "Here is the current situation. Decide for every coin in `coins`."
        if live:
            head += (" THIS ACCOUNT TRADES REAL MONEY on an exchange. Be selective; fees and small orders "
                     "matter, and the exchange has a minimum order of about $5.")
        return head + "\n\n" + json.dumps(payload, separators=(",", ":"))

    def decide(self, frames: dict, account: dict, risk: str, memory: dict, news: bool = False,
               live: bool = False) -> dict:
        coins = sorted(frames)
        briefing, news_cost, searches = self.news(coins) if news else (None, 0.0, 0)
        prompt = self.build_prompt(frames, account, risk, memory, briefing, live)
        response = self.client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=BETAS,
            fallbacks="default",  # if Claude Opus 5.5 declines, Anthropic retries on a fallback model
            system=SYSTEM,
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": schema(coins)}},
            messages=[{"role": "user", "content": prompt}],
        )
        cost, _ = usage_cost(response.usage)
        cost += news_cost
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined to answer this time; holding positions as they are.")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("Claude's answer was cut off; holding positions as they are.")
        text = next((b.text for b in reversed(response.content) if getattr(b, "type", "") == "text"), None)
        if not text:
            raise RuntimeError("Claude returned no decision; holding positions as they are.")
        data = json.loads(text)
        data["cost"] = cost
        data["searches"] = searches
        data["news"] = briefing
        data["model"] = getattr(response, "model", MODEL)
        return data


def to_targets(data: dict, coins: list[str], held: set[str], risk: str) -> tuple[dict, dict, list[dict]]:
    """Turn Claude's answer into safe targets: {coin: 1/0}, {coin: weight}, cleaned decisions."""
    cfg = RISK[risk]
    by_coin = {d["coin"]: d for d in data.get("decisions", []) if d.get("coin") in coins}
    targets, weights, cleaned = {}, {}, []
    budget = 1.0  # new buys only; the account's no-borrowing cap also applies when the orders are placed
    slots = cfg["max_coins"] - len([c for c in held if by_coin.get(c, {}).get("action") not in ("sell", "stay_out")])
    for c in coins:
        d = by_coin.get(c, {"coin": c, "action": "hold" if c in held else "stay_out", "size_pct": 0,
                            "confidence": "low", "reason": "No decision given; keeping things as they are."})
        action = d["action"]
        if c in held:
            keep = action not in ("sell", "stay_out")
            targets[c] = 1 if keep else 0
            if not keep:
                action = "sell"
            elif action == "buy":
                action = "hold"
        elif action == "buy" and slots > 0:
            w = min(max(float(d.get("size_pct") or 0), 0) / 100, cfg["per_coin"], budget)
            if w >= 0.01:
                targets[c], weights[c] = 1, w
                slots -= 1
                budget -= w
            else:
                targets[c], action = 0, "stay_out"
        else:
            if action == "buy":
                d = {**d, "reason": d["reason"] + f" (Not bought: max {cfg['max_coins']} coins at once.)"}
            targets[c], action = 0, "stay_out"
        cleaned.append({**d, "action": action})
    return targets, weights, cleaned
