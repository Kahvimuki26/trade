# tradebot

Backtest and paper-trade US stock strategies. It ships five classic strategies plus a
place to drop in ideas you find on TikTok. You can run many paper-trading
instances at once, each with its own strategy, symbols and simulated account.

> Paper trading only. Nothing here places real orders. Backtests are not a
> promise of future results, and most social-media strategies don't survive one.
> That's what this tool is for.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"            # add ,alpaca  for Alpaca data
pytest                              # optional sanity check
```

Data comes from Yahoo Finance by default, so no account is needed. To use Alpaca
data instead, copy `.env.example` to `.env`, fill in your free paper-account keys and
pass `--source alpaca` or set `data_source: alpaca`.

## Strategies

```bash
tradebot list
```

| name | idea |
|---|---|
| `buy_and_hold` | benchmark: always long |
| `sma_cross` | long while SMA20 > SMA50 |
| `rsi_reversion` | buy RSI < 30, sell RSI > 70 |
| `macd_cross` | long while MACD > signal line |
| `bollinger_reversion` | buy below the lower band, exit at the middle band |
| `donchian_breakout` | turtle: buy a 20-bar high, exit on a 10-bar low |
| `ema_cross_trend` | *TikTok example:* EMA9 > EMA21 and price above EMA200 |

### Adding a TikTok strategy

1. Copy `tradebot/strategies/social/_template.py` to e.g. `.../social/golden_rsi.py`.
2. Give it a unique `name`, the video in `source`, and the knobs in `default_params`.
3. Uncomment `@register` and write `generate_signals` (return 1 long / 0 flat / -1 short per bar).
4. Run `pytest`. It automatically checks every strategy for **look-ahead bias**
   (using future data), which is the #1 reason strategies look amazing in backtests and then fail live.
5. Run `tradebot compare SPY QQQ` and see whether it beats `buy_and_hold`.

Files are picked up automatically. There's no list to edit.

## Backtesting

```bash
tradebot backtest sma_cross SPY --start 2015-01-01
tradebot backtest donchian_breakout QQQ -p entry=55 -p exit=20 --trades
tradebot backtest macd_cross AAPL MSFT NVDA --out results/macd   # writes equity.csv, trades.csv
tradebot compare SPY                       # every strategy side by side, sorted by Sharpe
tradebot compare SPY QQQ -s sma_cross ema_cross_trend --sort total_return
tradebot backtest ema_cross_trend SPY --interval 1h --start 2024-06-01   # intraday
```

How the backtest works:

- A signal decided at bar *t*'s close is filled at bar *t+1*'s open, so nothing trades on prices it hasn't seen yet.
- Slippage defaults to 1 bp per fill. Commission defaults to 0. Change them with `--slippage-bps` and `--commission`.
- Equity is split equally across symbols. Fractional shares are on by default (`--whole-shares` turns them off).

Metrics reported: total return, CAGR, volatility, Sharpe, Sortino, max drawdown,
Calmar, win rate, profit factor, exposure, and the buy-and-hold return for comparison.

## Paper trading many instances

Define instances in `configs/instances.yaml`:

```yaml
instances:
  - name: sma-spy
    strategy: sma_cross
    params: {fast: 20, slow: 50}
    symbols: [SPY]
  - name: tiktok-ema-spy
    strategy: ema_cross_trend
    symbols: [SPY]
```

```bash
tradebot paper                      # run all enabled instances, polls every 5 min (Ctrl+C to stop)
tradebot paper -i sma-spy -i rsi-qqq   # only some
tradebot paper --once               # single pass, e.g. from cron
tradebot status                     # equity / return / positions of every instance
tradebot trades sma-spy -n 20       # trade log
tradebot reset sma-spy              # start over (old state backed up)
```

- Each instance has its own simulated account in `state/<name>.json`, so restarts keep positions.
  The paper trader uses the same accounting code as the backtester.
- **Daily bars** (`interval: 1d`): the bot acts once it's past `daily_trade_after`
  (15:45 New York time), so its decisions use almost the final daily bar.
- **Intraday bars** (`5m`, `15m`, `1h`): it acts on every poll, using only bars that have closed.
- It only trades during regular hours (Mon–Fri 9:30–16:00 ET). Exchange holidays
  aren't detected, but on a holiday prices don't move, so nothing changes. `--force` ignores market hours for testing.
- Want two instances to differ only by parameters? Give them different names and
  `params`. Changing a running instance's strategy logs a warning. Use `reset` for a clean start.
- Rules like "enter when X, exit when Y" depend on history. The live trader computes them
  over the fetched lookback window (`lookback_days`, about 450 days for daily bars).

To keep it running, use `tmux`/`screen`, a small VPS, or cron with `--once` every 5 minutes on weekdays.

## Layout

```
tradebot/
  strategies/
    base.py            Strategy interface, @register, latch()
    classic/           the built-in strategies
    social/            your TikTok/YouTube ideas (+ _template.py)
  indicators.py        sma, ema, rsi, macd, bollinger, atr, donchian, vwap
  data.py              yfinance / alpaca / csv providers
  portfolio.py         simulated account (shared by backtest + paper)
  backtest.py          backtest engine
  metrics.py           performance stats
  paper.py             multi-instance paper trader
  cli.py               `tradebot` command
configs/instances.yaml
tests/
```

## Ideas for next steps

- Stop-loss / take-profit and position sizing by volatility (ATR)
- Parameter sweeps and walk-forward testing (to avoid overfitting)
- Send orders to Alpaca's paper API as well (one instance per Alpaca account)
- Plot equity curves, or a small web dashboard for `status`
- Notifications (Discord/Telegram) on trades
