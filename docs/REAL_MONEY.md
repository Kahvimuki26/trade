# Let Claude trade real money (small amounts)

> **You can lose this money.** No AI can predict prices. Only use an amount you're fine losing completely.
> Start small (e.g. $15) and watch it for a day.

## What the bot can and can't do

- It places **real market orders** on your exchange account, for the coins you picked in the dashboard.
- It never spends more than **`LIVE_BUDGET`** (e.g. 15), even if your account holds more.
- It only sells coins **it bought itself**. Anything else you own is left alone.
- **Stop-loss:** checked every minute *while tradebot is open*. If your computer sleeps or tradebot is
  closed, there is no stop-loss. For the test day, set your PC to never sleep.
- **Loss limit:** if the bot's money falls `LIVE_MAX_LOSS_PCT`% (default 30%) below the budget, it sells
  everything and stops.
- **Red button:** "Sell everything & stop" sells all of the bot's positions immediately.
- It **cannot withdraw** anything, as long as your API key has no withdrawal permission (step 2).

## 1. Exchange account

Use an exchange you can legally use where you live, for example **Binance**, **Kraken**, **Coinbase** or
**Bybit**. Sign up, verify your identity, and deposit a little money.

The bot pays for coins with one currency, `LIVE_QUOTE`:

- **Binance in the EU:** use **USDC** (USDT isn't available to EU users). Deposit euros, then convert ~$15
  into USDC (Binance → Convert).
- **Kraken:** you can use **EUR** directly (`LIVE_QUOTE=EUR`), or USDC.
- Elsewhere: USDT or USDC usually work.

Minimum orders are about $5–10 per trade. With $15, use **Normal** or **Aggressive** risk, not Safe
(Safe makes $3 orders, which get skipped).

## 2. API key: trading only, NO withdrawals

In the exchange: Account → **API Management** → Create API key.

- Allow **reading** and **spot trading**.
- **Do NOT enable withdrawals.** (If you can, also restrict the key to your home IP address.)
- Copy the **API key** and **secret**. The secret is shown only once.

Never paste these into a chat, email or screenshot.

## 3. Put it in `.env`

Open the `.env` file in the tradebot folder and add:

```
LIVE_TRADING=yes
LIVE_EXCHANGE=binance
LIVE_API_KEY=paste-your-key
LIVE_API_SECRET=paste-your-secret
LIVE_QUOTE=USDC
LIVE_BUDGET=15
LIVE_MAX_LOSS_PCT=30
```

(Kraken: `LIVE_EXCHANGE=kraken`. Coinbase: `coinbase`. Bybit: `bybit`. Some exchanges also need
`LIVE_API_PASSWORD=` for the passphrase you chose.)

Your `ANTHROPIC_API_KEY` must be in `.env` too, for Claude.

## 4. Start

1. Close tradebot and open `start.bat` / `start.command` again.
2. A third tile appears: **"Claude, REAL MONEY"** (red). Click it.
3. Pick coins (e.g. BTC, ETH, SOL; they must exist as e.g. BTC/USDC on your exchange) and Normal risk.
4. Press **Start** and type `REAL` to confirm.
5. Keep the computer on and awake. Check in during the day; the activity feed shows every order and why.

When you're done: press **Sell everything & stop**, then set `LIVE_TRADING=no` in `.env`.

## Costs

- Exchange fees: about 0.1% per buy or sell.
- Claude: with news on and high thinking, roughly $0.20–0.40 per decision. Every 2 hours that's
  about $3–5/day. The **daily budget** setting (default $5) is a hard cap.

## Optional: practise on a test exchange first

Binance and Bybit have test environments with fake money. Create a key there
(Binance: testnet.binance.vision), and set `LIVE_TESTNET=yes`.
