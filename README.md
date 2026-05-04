# polymarket-btc5m

Contrarian live trading bot for Polymarket's **Bitcoin Up or Down — 5 minutes** markets.

## Strategy

At 5m scale BTC mean-reverts, not continues. The bot bets **against** strong prior-bar moves and
against crowded same-direction flow.

| Tier | Trigger | Size | Historical WR |
|---|---|---|---|
| A | `|z_prev| > 3σ` | 2.0% bankroll | ~59% |
| C | `1h trend ± 0.5%` + aligned taker-buy | 1.0% bankroll | ~56% |

Filters: skip 03–06 UTC; skip vol-spikes `|vol_z|>4`; skip spread > 4c; skip same-side depth < $300.
Bear-market regime filter (4h return < -1.5%) blocks UP bets.

## Files

| File | Role |
|---|---|
| `live_trade.py` | Live trader — CLOB orders, on-chain redeem, bankroll reconcile vs chain |
| `signal_lib.py` | Signal + market-data library (book walk, Polymarket resolution, Binance outcome) |
| `claim_sweeper.py` | Background service: redeems winning + losing tokens via Safe execTransaction; wraps USDC.e → pUSD |
| `pm_chain.py` | Polygon RPC + CTF / Safe contract bindings |
| `pm_clob.py` | Polymarket CLOB v2 client wrapper |
| `reconcile.py` | Standalone bankroll vs on-chain reconcile tool |
| `setup_wallet.py`, `setup_approvals.py` | One-time wallet + approvals bootstrap |
| `bridge_to_safe.py` | Top up the Polymarket Safe via Relay (USDC.e → pUSD) |
| `download.py` | Pull 6mo of Binance BTCUSDT 5m klines |
| `diagnose.py` | Feature-by-feature win-rate scan |
| `collect_polymarket.py` | Pull Polymarket CLOB trades via Gamma + Data APIs |
| `dune_query.sql` | Dune Analytics query for on-chain CTF-Exchange fills |

## Run

The live bot runs as the Railway service `polymarket-btc5m-live`. Push to `live-trader-v1`
and `railway redeploy --service polymarket-btc5m-live` to deploy.

## Real-world factors modeled

- **Chainlink resolution** via Polymarket Gamma `outcomePrices` (Binance recorded as control)
- **Full book walk** with VWAP fill, top-of-book slippage, levels consumed
- **Depth cap**: max 10% of same-side notional depth
- **Spread filter**: skip if `best_ask − best_bid > 4c`
- **Liquidity filter**: skip if same-side depth < $300
- **Bankroll reconcile**: at boot and after each FINAL, the bot verifies its
  internal bankroll against on-chain pUSD on the Safe + locked stake in any
  open position. Drift logged as `RECONCILE`.
- **ISP bypass**: DoH resolution of `*.polymarket.com` (India ISPs sinkhole it)

## Data source for resolution

Polymarket 5m markets resolve on the [Chainlink BTC/USD data stream](https://data.chain.link/streams/btc-usd)
(aggregated: Binance + Coinbase + Kraken). Binance BTCUSDT 5m OHLC is used for the signal and as
a control outcome — the bot tracks how often the two disagree.

## Not included in this repo

Per `.gitignore`: live bot state, tick log, downloaded klines, collected Polymarket trade dumps.
These are regenerable and big.
