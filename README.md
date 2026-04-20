# polymarket-btc5m

Contrarian paper-trading bot + backtest harness for Polymarket's **Bitcoin Up or Down — 5 minutes** markets.

## Strategy

At 5m scale BTC mean-reverts, not continues. The bot bets **against** strong prior-bar moves and
against crowded same-direction flow.

| Tier | Trigger | Size | Historical WR |
|---|---|---|---|
| A | `|z_prev| > 3σ` | 2.0% bankroll | ~59% |
| B | `|z_prev| > 2σ` | 1.5% bankroll | ~54% |
| C | `1h trend ± 0.5%` + aligned taker-buy | 1.0% bankroll | ~56% |

Filters: skip 03–06 UTC; skip vol-spikes `|vol_z|>4`; skip spread > 4c; skip same-side depth < $300.

## Files

| File | Role |
|---|---|
| `paper_trade.py` | Live paper bot — full orderbook walk, Polymarket resolution, latency model, depth-scaled stake, partial fills |
| `status.py` | Status reader (bankroll, WR, DD, basis flips, slippage stats) |
| `download.py` | Pull 6mo of Binance BTCUSDT 5m klines |
| `diagnose.py` | Feature-by-feature win-rate scan |
| `backtest.py`, `backtest_v2.py`, `backtest_final.py` | Historical backtest variants |
| `collect_polymarket.py` | Pull Polymarket CLOB trades via Gamma+Data APIs (needs non-India DNS) |
| `record_live.py` | Live CLOB websocket recorder |
| `backtest_polymarket.py` | Replay strategy on real Polymarket fills |
| `dune_query.sql` | Dune Analytics query for on-chain CTF-Exchange fills |

## Run the paper bot

```bash
source venv/bin/activate
nohup caffeinate -ims python paper_trade.py > paper/bot.out 2>&1 & disown
python status.py           # anytime
tail -f paper/tick.log
```

## Real-world factors modeled

- **Chainlink resolution** via Polymarket Gamma `outcomePrices` (Binance recorded as control)
- **Full book walk** with VWAP fill, top-of-book slippage, levels consumed
- **Depth cap**: max 10% of same-side notional depth
- **Partial fills** flagged when filled_ratio < 0.5
- **Latency offset**: median India→Polymarket RTT (~250ms) shifts observation time
- **Spread filter**: skip if `best_ask − best_bid > 4c`
- **Liquidity filter**: skip if same-side depth < $300
- **Gas**: flat $0.01 per trade
- **Basis flips**: tracks Chainlink ≠ Binance outcome disagreements
- **ISP bypass**: DoH resolution of `*.polymarket.com` (India ISPs sinkhole it)

## Data source for resolution

Polymarket 5m markets resolve on the [Chainlink BTC/USD data stream](https://data.chain.link/streams/btc-usd)
(aggregated: Binance + Coinbase + Kraken). Binance BTCUSDT 5m OHLC is used for the signal and as
a control outcome — the bot tracks how often the two disagree.

## Not included in this repo

Per `.gitignore`: live bot state, tick log, downloaded klines, collected Polymarket trade dumps.
These are regenerable and big.
