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
data/bot.json                your bot's money, settings and history (never overwritten by updates)
.env                         optional: TRADEBOT_PORT, TRADEBOT_PASSWORD
tradebot/
  autopilot.py   playbooks + the brain that picks between them
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
