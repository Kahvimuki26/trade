"""REAL-MONEY trading on a crypto exchange (via the ccxt library).

Off unless .env says LIVE_TRADING=yes. Safety rules, all enforced here in code:
  * It only ever uses LIVE_BUDGET (e.g. $15) of the exchange balance, even if there is more.
  * It only sells coins it bought itself; anything else in the account is left alone.
  * Orders below the exchange minimum are skipped, never forced.
  * If the account value (budget part) falls LIVE_MAX_LOSS_PCT below the start, it sells
    everything and stops.
  * Use exchange API keys WITHOUT withdrawal permission.

.env settings:
  LIVE_TRADING=yes
  LIVE_EXCHANGE=binance          (binance, kraken, coinbase, bybit, ... any ccxt exchange id)
  LIVE_API_KEY=...               LIVE_API_SECRET=...   (LIVE_API_PASSWORD for exchanges that need one)
  LIVE_QUOTE=USDC                (what you pay with: USDC, USDT, EUR, USD - must exist on the exchange)
  LIVE_BUDGET=15
  LIVE_MAX_LOSS_PCT=30
  LIVE_TESTNET=no                (yes = exchange's test environment, if it has one)
"""

from __future__ import annotations

import logging
import os
import time

from .portfolio import Portfolio, Trade

log = logging.getLogger("tradebot")


def live_config() -> dict | None:
    if os.environ.get("LIVE_TRADING", "").strip().lower() not in ("yes", "true", "1"):
        return None
    return {
        "exchange": os.environ.get("LIVE_EXCHANGE", "binance").strip().lower(),
        "key": os.environ.get("LIVE_API_KEY", "").strip(),
        "secret": os.environ.get("LIVE_API_SECRET", "").strip(),
        "password": os.environ.get("LIVE_API_PASSWORD", "").strip() or None,
        "quote": os.environ.get("LIVE_QUOTE", "USDC").strip().upper(),
        "budget": float(os.environ.get("LIVE_BUDGET", "15") or 15),
        "max_loss": float(os.environ.get("LIVE_MAX_LOSS_PCT", "30") or 30) / 100,
        "testnet": os.environ.get("LIVE_TESTNET", "").strip().lower() in ("yes", "true", "1"),
    }


def connect(cfg: dict):
    import ccxt

    if not cfg["key"] or not cfg["secret"]:
        raise RuntimeError("Add LIVE_API_KEY and LIVE_API_SECRET (from your exchange) to .env")
    cls = getattr(ccxt, cfg["exchange"], None)
    if cls is None:
        raise RuntimeError(f"Unknown exchange '{cfg['exchange']}'")
    params = {"apiKey": cfg["key"], "secret": cfg["secret"], "enableRateLimit": True}
    if cfg["password"]:
        params["password"] = cfg["password"]
    ex = cls(params)
    if cfg["testnet"]:
        ex.set_sandbox_mode(True)
    ex.load_markets()
    return ex


class LivePortfolio(Portfolio):
    """Same bookkeeping as the play-money account, but every order is a real exchange order."""

    def __init__(self, exchange, quote: str, budget: float, max_loss: float = 0.3, **state):
        state.setdefault("cash", budget)
        super().__init__(slippage_bps=0.0, commission=0.0, fractional=True, **state)
        self.ex, self.quote, self.budget, self.max_loss = exchange, quote, budget, max_loss
        self.notes: list[str] = []  # messages for the bot's activity feed

    def pair(self, coin: str) -> str:
        return f"{coin}/{self.quote}"

    def check_coins(self, coins: list[str]) -> list[str]:
        missing = [c for c in coins if self.pair(c) not in self.ex.markets]
        if missing:
            raise RuntimeError(f"{', '.join(self.pair(c) for c in missing)} can't be traded on "
                               f"{self.ex.id}. Remove those coins or change LIVE_QUOTE.")
        return coins

    def min_cost(self, coin: str, price: float | None = None) -> float:
        """Smallest order the exchange accepts, in quote currency (checks both $ and coin minimums)."""
        limits = self.ex.market(self.pair(coin)).get("limits") or {}
        cost_min = (limits.get("cost") or {}).get("min")
        amount_min = (limits.get("amount") or {}).get("min")
        mins = [float(cost_min)] if cost_min else []
        if amount_min and price:
            mins.append(float(amount_min) * price * 1.01)
        return max(mins) if mins else 5.0

    def live_prices(self, coins: list[str]) -> dict[str, float]:
        tickers = self.ex.fetch_tickers([self.pair(c) for c in coins])
        out = {}
        for c in coins:
            t = tickers.get(self.pair(c)) or {}
            p = t.get("last") or t.get("close") or t.get("bid")
            if p:
                out[c] = float(p)
        return out

    def free(self, currency: str) -> float:
        bal = self.ex.fetch_balance()
        return float((bal.get(currency) or {}).get("free") or 0.0)

    def _fill(self, order: dict) -> dict:
        """Wait briefly for a market order's fill details."""
        for _ in range(10):
            if order.get("status") in ("closed", "canceled") and order.get("filled"):
                return order
            time.sleep(0.5)
            order = self.ex.fetch_order(order["id"], order.get("symbol"))
        return order

    def order(self, symbol: str, qty: float, price: float, ts) -> Trade | None:
        qty, price = float(qty), float(price)
        if qty == 0:
            return None
        pair = self.pair(symbol)
        if qty > 0:
            cost = min(qty * price, self.cash, self.free(self.quote))
            if cost < self.min_cost(symbol, price):
                self.notes.append(f"Skipped buying {symbol}: {cost:,.2f} {self.quote} is below {self.ex.id}'s minimum "
                                  f"order of {self.min_cost(symbol, price):,.2f} {self.quote}.")
                return None
            o = self._fill(self.ex.create_market_buy_order_with_cost(pair, round(cost, 2)))
        else:
            available = min(-qty, self.positions.get(symbol, 0.0), self.free(symbol))
            amount = float(self.ex.amount_to_precision(pair, available)) if available > 0 else 0.0
            if amount > available:  # precision rounded up -> never try to sell more than we have
                amount = float(self.ex.amount_to_precision(pair, available * 0.999))
            if amount <= 0 or amount * price < self.min_cost(symbol, price) * 0.5:
                # too small to sell ("dust"): stop tracking it so it doesn't block anything
                self.positions.pop(symbol, None)
                self.avg_price.pop(symbol, None)
                self.notes.append(f"{symbol}: leftover too small to sell, no longer tracked.")
                return None
            o = self._fill(self.ex.create_market_sell_order(pair, amount))

        filled = float(o.get("filled") or 0.0)
        if filled <= 0:
            self.notes.append(f"{symbol} order didn't fill (status: {o.get('status')}).")
            return None
        spent = float(o.get("cost") or filled * float(o.get("average") or price))
        avg = spent / filled
        fee_quote = fee_base = 0.0
        for f in o.get("fees") or ([o["fee"]] if o.get("fee") else []):
            if not f or f.get("cost") is None:
                continue
            if f.get("currency") == self.quote:
                fee_quote += float(f["cost"])
            elif f.get("currency") == symbol:
                fee_base += float(f["cost"])

        cur = self.positions.get(symbol, 0.0)
        realized = None
        if qty > 0:
            got = filled - fee_base
            new = cur + got
            self.avg_price[symbol] = (self.avg_price.get(symbol, 0.0) * cur + spent + fee_quote) / new
            self.positions[symbol] = new
            self.cash -= spent + fee_quote
            signed = got
        else:
            received = spent - fee_quote
            realized = received - self.avg_price.get(symbol, avg) * filled
            new = cur - filled
            if new * avg < self.min_cost(symbol, avg) * 0.5:
                self.positions.pop(symbol, None)
                self.avg_price.pop(symbol, None)
            else:
                self.positions[symbol] = new
            self.cash += received
            signed = -filled
        t = Trade(str(ts), symbol, signed, avg, fee_quote + fee_base * avg, realized)
        self.trades.append(t)
        log.info("LIVE %s %s %.8f @ %.6f", "BUY" if qty > 0 else "SELL", pair, filled, avg)
        return t

    def reconcile(self) -> None:
        """If coins were sold outside the bot, stop counting what's no longer there."""
        bal = self.ex.fetch_balance()
        for c in list(self.positions):
            have = float((bal.get(c) or {}).get("total") or 0.0)
            if have < self.positions[c] * 0.98:
                self.notes.append(f"{c}: only {have:g} left on the exchange (sold outside the bot?), adjusting.")
                if have <= 0:
                    self.positions.pop(c, None)
                    self.avg_price.pop(c, None)
                else:
                    self.positions[c] = have
        self.cash = min(self.cash, float((bal.get(self.quote) or {}).get("free") or 0.0))

    def sell_all(self, prices: dict, ts) -> list[Trade]:
        trades = []
        for c in list(self.positions):
            t = self.order(c, -self.positions[c], prices.get(c, self.avg_price.get(c, 0)), ts)
            if t:
                trades.append(t)
        return trades

    def loss_limit_hit(self, prices: dict) -> bool:
        return self.current_equity(prices) < self.budget * (1 - self.max_loss)

    def to_dict(self) -> dict:
        return {"cash": self.cash, "positions": self.positions, "avg_price": self.avg_price,
                "trades": [t.__dict__ for t in self.trades]}

    @classmethod
    def restore(cls, exchange, quote: str, budget: float, max_loss: float, d: dict | None) -> "LivePortfolio":
        if not d:
            return cls(exchange, quote, budget, max_loss)
        return cls(exchange, quote, budget, max_loss, cash=d.get("cash", budget),
                   positions=d.get("positions", {}), avg_price=d.get("avg_price", {}),
                   trades=[Trade(**t) for t in d.get("trades", [])])
