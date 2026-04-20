"""
Polymarket BTC 5m Up/Down backtest — "Fair-value + flow overlay" strategy.

Design notes:
- Polymarket resolves on Chainlink BTC/USD (aggregates Binance/Coinbase/Kraken).
  Binance BTCUSDT 5m OHLC is within a few bps of what Chainlink prints at
  window start/end, so it's a faithful proxy.
- Without tick-level orderbook/CVD/liq data we build signals from what a 5m
  bar actually exposes: prior-bar return, taker-buy ratio (aggressive buyer
  share — a 5m CVD proxy), realized volatility, trend, volume regime, ToD.
- Rules are set from microstructure principles, not fit to this data, to
  keep the backtest out-of-sample-like. Walk-forward calibration would tune
  weights in production.

Polymarket cost model (realistic, conservative):
- Trading fees: 0% (Polymarket dropped them in 2024).
- Bid/ask spread: ~1–2c on 5m markets.
- Retail bias: momentum-aligned side carries a 1–2c premium; contrarian side
  carries a 1–2c discount.
- Effective entry price: 0.52 when signal aligns with prior-bar direction
  (momentum trade), 0.48 when contrarian, 0.50 if neutral.
- Slippage: modeled as an extra 0.5c worst-case on entry.
- Settlement: binary, $1 per winning share, $0 per losing share.
"""
import numpy as np
import pandas as pd
import pathlib

ROOT = pathlib.Path(__file__).parent
df = pd.read_csv(ROOT / "btcusdt_5m.csv", parse_dates=["open_time", "close_time"])
df = df.sort_values("open_time").reset_index(drop=True)

# ---- Feature engineering (strictly using bars up to N-1 for bar-N decision) ----
df["ret"]       = df["close"] / df["open"] - 1.0
df["buy_ratio"] = df["taker_buy_base"] / df["volume"].replace(0, np.nan)
df["hour"]      = df["open_time"].dt.hour

# Lagged features (decision at open of bar N uses bars <= N-1)
df["ret_1"]     = df["ret"].shift(1)
df["ret_3"]     = (df["close"].shift(1) / df["close"].shift(4) - 1.0)
df["br_1"]      = df["buy_ratio"].shift(1)
df["vol_1"]     = df["volume"].shift(1)

# Realized vol over last 24 bars (2 hours) as a normalizer
df["sigma"]     = df["ret"].shift(1).rolling(24).std()
df["z_prev"]    = df["ret_1"] / df["sigma"]

# EMA-20 trend (using bars <= N-1)
df["ema20"]     = df["close"].shift(1).ewm(span=20, adjust=False).mean()
df["trend"]     = np.sign(df["close"].shift(1) - df["ema20"])

# Volume z-score over last 288 bars (1 day)
df["vol_ma"]    = df["vol_1"].rolling(288).mean()
df["vol_sd"]    = df["vol_1"].rolling(288).std()
df["vol_z"]     = (df["vol_1"] - df["vol_ma"]) / df["vol_sd"]

# Outcome: did bar N close above its open? (the Polymarket binary resolution)
df["y"]         = np.where(df["close"] > df["open"], 1, -1)

df = df.dropna().reset_index(drop=True)

# ---- Signal construction (from first-principles microstructure weights) ----
# 1. Short-horizon continuation when last bar is "normal" (weak positive autocorr at 5m)
s1 = np.where(df["z_prev"].abs() < 1.5, 0.5 * np.sign(df["ret_1"]), 0.0)

# 2. Mean-reversion on extreme moves (stops get hit, then price reverts)
s2 = np.where(df["z_prev"].abs() > 2.0, -0.4 * np.sign(df["ret_1"]), 0.0)

# 3. Aggressive-buyer dominance (taker-buy share deviation from 0.5)
s3 = 2.0 * (df["br_1"] - 0.5)  # range roughly -0.3 to +0.3

# 4. Trend alignment: reward when prior move is WITH the EMA trend
s4 = 0.3 * df["trend"] * np.sign(df["ret_1"])

# 5. 3-bar momentum nudge (tiny, to catch gentle trends)
s5 = 0.2 * np.sign(df["ret_3"])

df["score"] = s1 + s2 + s3 + s4 + s5

# ---- Filters ----
# Skip the thinnest hours — 03:00–06:00 UTC, Asia/EU handoff, very noisy at 5m
LOW_LIQ_HOURS = {3, 4, 5}
df["skip_hour"]   = df["hour"].isin(LOW_LIQ_HOURS)
# Skip when the prior bar was a vol outlier (we don't trust our vol norm)
df["skip_volext"] = df["vol_z"].abs() > 3.0
# Skip when signal is weak
THRESHOLD = 0.35
df["has_signal"]  = df["score"].abs() >= THRESHOLD
df["trade"]       = df["has_signal"] & ~df["skip_hour"] & ~df["skip_volext"]
df["direction"]   = np.sign(df["score"])  # +1 UP, -1 DOWN

# ---- P&L simulation ----
# Retail bias: market momentum-aligned side costs more.
# Our "momentum-aligned" = direction matches prior bar's direction.
aligned = (df["direction"] == np.sign(df["ret_1"]))
contrarian = (df["direction"] == -np.sign(df["ret_1"]))
# Entry price on our side:
entry_price = np.where(aligned, 0.525,
                np.where(contrarian, 0.485, 0.505))
# Add slippage:
entry_price = entry_price + 0.005

df["entry_price"] = entry_price
df["win"]  = df["trade"] & (df["direction"] == df["y"])
df["loss"] = df["trade"] & (df["direction"] != df["y"])

# Sizing: fractional Kelly on a 54% assumed WR at 0.525 entry is ~3%; we
# use a fixed 4% of current bankroll — simple, robust.
STAKE_FRAC = 0.04
START_BANKROLL = 100.0

bankroll = START_BANKROLL
equity_curve = []
wins = 0
losses = 0
trades = 0
stakes = []

for i, row in df.iterrows():
    if not row["trade"]:
        equity_curve.append((row["open_time"], bankroll))
        continue
    stake = bankroll * STAKE_FRAC
    ep = row["entry_price"]
    # shares bought = stake / ep. Win payoff = shares * $1 - stake
    if row["direction"] == row["y"]:
        pnl = stake * (1.0 / ep - 1.0)  # win
        wins += 1
    else:
        pnl = -stake  # lose entire stake
        losses += 1
    trades += 1
    bankroll += pnl
    stakes.append(stake)
    equity_curve.append((row["open_time"], bankroll))

eq = pd.DataFrame(equity_curve, columns=["ts", "equity"]).set_index("ts")
eq["drawdown"] = eq["equity"] / eq["equity"].cummax() - 1.0

# ---- Reporting ----
n_bars = len(df)
wr = wins / trades if trades else 0
total_return = (bankroll / START_BANKROLL - 1.0) * 100
max_dd = eq["drawdown"].min() * 100
avg_stake = np.mean(stakes) if stakes else 0

print("=" * 70)
print("POLYMARKET BTC 5m — BACKTEST RESULTS")
print("=" * 70)
print(f"Data range       : {df['open_time'].iloc[0]}  →  {df['open_time'].iloc[-1]}")
print(f"Total 5m bars    : {n_bars:,}")
print(f"Trades taken     : {trades:,}  ({trades/n_bars:.1%} of bars)")
print(f"Wins             : {wins:,}")
print(f"Losses           : {losses:,}")
print(f"Win rate         : {wr:.3%}")
print(f"Avg stake        : ${avg_stake:.3f}")
print(f"Starting bankroll: ${START_BANKROLL:.2f}")
print(f"Ending bankroll  : ${bankroll:.2f}")
print(f"Total return     : {total_return:+.2f}%")
print(f"Max drawdown     : {max_dd:.2f}%")
print()

# Monthly breakdown
eq2 = eq.copy()
eq2["month"] = eq2.index.to_period("M")
monthly = eq2.groupby("month")["equity"].agg(["first", "last"])
monthly["return_%"] = (monthly["last"] / monthly["first"] - 1.0) * 100
monthly["eop_equity"] = monthly["last"]

df2 = df.copy()
df2["month"] = df2["open_time"].dt.to_period("M")
trade_stats = df2[df2["trade"]].groupby("month").agg(
    trades=("trade", "sum"),
    wins=("win", "sum"),
)
trade_stats["wr_%"] = trade_stats["wins"] / trade_stats["trades"] * 100

combined = monthly[["return_%", "eop_equity"]].join(trade_stats, how="left")
combined = combined.fillna(0)
print("MONTHLY PERFORMANCE")
print("-" * 70)
print(f"{'Month':<10}{'Trades':>8}{'Wins':>8}{'WR %':>10}{'Return %':>12}{'Equity':>12}")
print("-" * 70)
for m, row in combined.iterrows():
    print(f"{str(m):<10}{int(row['trades']):>8}{int(row['wins']):>8}"
          f"{row['wr_%']:>10.2f}{row['return_%']:>12.2f}{row['eop_equity']:>12.2f}")
print("-" * 70)

# Save equity curve for reference
eq.to_csv(ROOT / "equity_curve.csv")
