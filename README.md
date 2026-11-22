# tradebot: crypto autopilot

A crypto trading bot that **paper-trades** (play money) on **live prices**, 24/7.
You don't pick a strategy: every hour the autopilot checks which of its playbooks has
actually been working lately on each coin, uses that one, and puts the money into the
coins that look best. Every decision comes with a plain-English reason.

> Play money only. Nothing is ever bought for real. No bot can predict the market;
> use "Test it on the past" and let it run for a few weeks before trusting it.

## Start it

1. Install Python from python.org (Windows: tick **"Add Python to PATH"**).
2. Double-click **`start.bat`** (Windows) or **`start.command`** (Mac; if blocked, right-click → Open).
3. Your browser opens the dashboard at http://127.0.0.1:8765 (or the next free port).
   Pick coins and a risk level, then press **Start**. Keep the window open.

It **updates itself** every time you start it, and shows an "Update now" bar when a new
version comes out. Your money, settings and history (in `data/`) and your `.env` are never touched.
If you close the window while the bot is running, it continues when you open it again.

## Two traders, side by side

The two tiles at the top are two separate play-money accounts, so you can compare them:

- **Autopilot:** rules that pick their own playbook every hour (below). Free.
- **Claude AI:** every hour (or 2/4/8 h, your choice) the bot sends Claude (`claude-opus-5-5`)
  a summary of each coin, your positions and Claude's own notes from last time. Claude answers
  with buy / sell / hold / stay out per coin, a size, a confidence and a plain-English reason.
  The code enforces the limits (position caps, no borrowing, stop-loss checked every minute), so
  Claude can't break them. Needs an Anthropic API key: create one at console.anthropic.com,
  add `ANTHROPIC_API_KEY=...` to `.env`, and restart. Each decision costs a few cents; the
  dashboard shows the running total. Claude can't be "tested on the past" fairly, because it may
  remember what prices did, so compare it live against the Autopilot.

**News + deeper thinking:** before each decision Claude searches the web for fresh crypto news
(market mood, regulation, ETF flows, hacks...) and then decides at high effort. Default: every 2 hours,
with a **daily budget** (default $5) that hard-caps Claude's API spend.

**Real money (optional, off by default):** a third tile, "Claude, REAL MONEY", appears when `.env` has
`LIVE_TRADING=yes` and exchange API keys. It places real orders on your exchange but never uses more than
`LIVE_BUDGET`, only sells what it bought, sells everything and stops at a loss limit, and has a red
"Sell everything & stop" button. **Read [docs/REAL_MONEY.md](docs/REAL_MONEY.md) first.**

## How the autopilot decides

| Playbook | What it does |
|---|---|
| Ride the trend | Holds while the price is in an uptrend (20-hour average above 50-hour average) |
| Catch breakouts | Buys a break above the 2-day high, sells a break below the 1-day low |
| Buy the dips | Buys unusually sharp drops, sells once the price is back to normal |
| Sit in cash | When none of the above has made money lately |

Every hour, for each coin: replay each playbook over the last 1–3 weeks (including 0.12% fees
per trade), use the best one, rank the coins, and hold the top 1–3. Every position has a stop-loss.

| Risk | Coins at once | Per coin | Stop-loss | Judges playbooks on |
|---|---|---|---|---|
| Safe | 3 | 20% | 4% | 3 weeks |
| Normal | 2 | 50% | 6% | 2 weeks |
| Aggressive | 1 | 100% | 8% | 1 week |

Prices are hourly candles from Binance's free public API (no account needed). If Binance is
blocked where you live, the bot shows an error on the dashboard.

## Files

```
start.bat / start.command   double-click to run (sets itself up, updates itself)
data/bot.json, claude.json   each trader's money, settings and history (never overwritten by updates)
.env                         ANTHROPIC_API_KEY (for Claude), optional TRADEBOT_PORT, TRADEBOT_PASSWORD
tradebot/
  autopilot.py   playbooks + the brain that picks between them
  ai.py          Claude as the trader (news research, decision, safety limits)
  live.py        real-money orders on an exchange (ccxt), budget + loss limits
  bot.py         the live paper-trading loop
  sim.py         "test it on the past"
  data.py        Binance prices (+ made-up prices for tests)
  portfolio.py   play-money account
  web/           the one-screen dashboard
  updater.py, launch.py   self-update + launcher
docs/CLOUDFLARE.md   put the dashboard on your own domain
```

Terminal users: `pip install -e ".[dev]"`, then `tradebot web --open`,
`tradebot test --days 90 --risk normal --coins BTC ETH SOL`, or `pytest`.
