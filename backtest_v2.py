"""
Polymarket BTC 5m Up/Down backtest — v2, CONTRARIAN / MEAN-REVERSION.

Diagnostic finding (see diagnose.py):
  At 5m BTC scale, short-horizon returns are NEGATIVELY autocorrelated.
  Extreme moves (|z_prev| > 2σ) revert with ~55% probability; |z| > 3
  reverts with ~58%. Aggressive-buyer dominance (high taker-buy ratio)
  combined with 1h trend also flags crowded trades that mean-revert.

Strategy (entirely derived from signed-edge signals, not fit to P&L):
  Tier A  (strongest): |z_prev| > 3  →  bet AGAINST last-bar direction
  Tier B  (moderate):  |z_prev| > 2  →  bet against (unless Tier A hit)
  Tier C  (moderate):  1h trend > 0.5% AND taker-buy > 0.55   →  bet DOWN
                       1h trend < -0.5% AND taker-buy < 0.45  →  bet UP
  Filters:
    - Skip 03:00–06:00 UTC (thin liquidity)
    - Skip when prior-bar volume z-score > 4 (data spike / flash event)

Pricing model (realistic Polymarket):
  - Fees: 0% (Polymarket CLOB).
  - Retail bias: contrarian side trades at ~0.48. Slippage 0.5c.
  - Effective entry price = 0.485 for contrarian bets.
  - Break-even WR = 0.485.  Any WR above that prints money.

Sizing: tiered fractional Kelly
  Tier A: 8% of bankroll
  Tier B: 5% of bankroll
  Tier C: 3% of bankroll
"""
import numpy as np
import pandas as pd
import pathlib

ROOT = pathlib.Path(__file__).parent
df = pd.read_csv(ROOT / "btcusdt_5m.csv", parse_dates=["open_time", "close_time"])
df = df.sort_values("open_time").reset_index(drop=True)

df["ret"]       = df["close"] / df["open"] - 1.0
df["buy_ratio"] = df["taker_buy_base"] / df["volume"].replace(0, np.nan)
df["hour"]      = df["open_time"].dt.hour
df["y"]         = np.where(df["close"] > df["open"], 1, -1)

df["ret_1"]     = df["ret"].shift(1)
df["ret_12"]    = df["close"].shift(1) / df["close"].shift(13) - 1.0
df["br_1"]      = df["buy_ratio"].shift(1)
df["vol_1"]     = df["volume"].shift(1)
df["sigma"]     = df["ret"].shift(1).rolling(24).std()
df["z_prev"]    = df["ret_1"] / df["sigma"]
df["vol_ma"]    = df["vol_1"].rolling(288).mean()
df["vol_sd"]    = df["vol_1"].rolling(288).std()
df["vol_z"]     = (df["vol_1"] - df["vol_ma"]) / df["vol_sd"]
df = df.dropna().reset_index(drop=True)

LOW_LIQ_HOURS = {3, 4, 5}

def classify(row):
    if row["hour"] in LOW_LIQ_HOURS:          return (None, 0.0)
    if abs(row["vol_z"]) > 4:                 return (None, 0.0)
    z = row["z_prev"]
    # Tier A: extreme reversion
    if z >  3.0:   return (-1, 0.08)
    if z < -3.0:   return (+1, 0.08)
    # Tier B: strong reversion
    if z >  2.0:   return (-1, 0.05)
    if z < -2.0:   return (+1, 0.05)
    # Tier C: crowded-trade contrarian (1h trend + aggressive taker in same direction)
    if row["ret_12"] >  0.005 and row["br_1"] > 0.55: return (-1, 0.03)
    if row["ret_12"] < -0.005 and row["br_1"] < 0.45: return (+1, 0.03)
    return (None, 0.0)

tiers  = df.apply(classify, axis=1)
df["direction"] = tiers.apply(lambda t: t[0])
df["size_frac"] = tiers.apply(lambda t: t[1])
df["trade"]     = df["direction"].notna()

# Entry price model: contrarian bets ride the cheap side
CONTRARIAN_PRICE = 0.485
SLIPPAGE         = 0.005
ENTRY_PRICE      = CONTRARIAN_PRICE + SLIPPAGE   # 0.49

START_BANKROLL = 100.0
bankroll = START_BANKROLL
equity_curve = []
wins = losses = trades = 0
tier_stats = {"A": [0, 0], "B": [0, 0], "C": [0, 0]}
pnls = []

for i, row in df.iterrows():
    if not row["trade"]:
        equity_curve.append((row["open_time"], bankroll))
        continue
    stake = bankroll * row["size_frac"]
    direction = int(row["direction"])
    outcome   = int(row["y"])
    # Tier label for stats
    sf = row["size_frac"]
    tier = "A" if sf == 0.08 else ("B" if sf == 0.05 else "C")
    tier_stats[tier][0] += 1
    if direction == outcome:
        pnl = stake * (1.0 / ENTRY_PRICE - 1.0)
        wins += 1
        tier_stats[tier][1] += 1
    else:
        pnl = -stake
        losses += 1
    trades += 1
    bankroll += pnl
    pnls.append(pnl)
    equity_curve.append((row["open_time"], bankroll))

eq = pd.DataFrame(equity_curve, columns=["ts", "equity"]).set_index("ts")
eq["drawdown"] = eq["equity"] / eq["equity"].cummax() - 1.0

print("=" * 74)
print("POLYMARKET BTC 5m — CONTRARIAN BACKTEST (v2)")
print("=" * 74)
print(f"Data range       : {df['open_time'].iloc[0]}  →  {df['open_time'].iloc[-1]}")
print(f"Total 5m bars    : {len(df):,}")
print(f"Trades taken     : {trades:,}  ({trades/len(df):.1%} of bars)")
print(f"Win rate         : {wins/trades:.3%}")
print(f"Break-even WR    : {ENTRY_PRICE:.3%}  (edge per trade: "
      f"{(wins/trades/ENTRY_PRICE - 1)*100:+.2f}%)")
print()
print("By tier:")
for t in ("A", "B", "C"):
    n, w = tier_stats[t]
    size_pct = {"A": 8, "B": 5, "C": 3}[t]
    rule    = {"A":"|z|>3","B":"|z|>2","C":"trend+tbd"}[t]
    if n:
        print(f"  Tier {t} ({rule:9s} @ {size_pct}%): {n:>6} trades, "
              f"WR {w/n:.2%}, edge {(w/n/ENTRY_PRICE-1)*100:+.2f}%/trade")
print()
print(f"Starting bankroll: ${START_BANKROLL:.2f}")
print(f"Ending bankroll  : ${bankroll:.2f}")
print(f"Total return     : {(bankroll/START_BANKROLL-1)*100:+.2f}%")
print(f"Max drawdown     : {eq['drawdown'].min()*100:.2f}%")
print()

eq2 = eq.copy()
eq2.index = pd.to_datetime(eq2.index).tz_convert("UTC").tz_localize(None)
eq2["month"] = eq2.index.to_period("M")
monthly = eq2.groupby("month")["equity"].agg(["first", "last"])
monthly["return_%"] = (monthly["last"] / monthly["first"] - 1.0) * 100

df2 = df.copy()
df2["month"] = df2["open_time"].dt.tz_convert("UTC").dt.tz_localize(None).dt.to_period("M")
df2["win"]   = df2["trade"] & (df2["direction"] == df2["y"])
trade_stats  = df2[df2["trade"]].groupby("month").agg(
    trades=("trade", "sum"),
    wins=("win", "sum"),
)
trade_stats["wr_%"] = trade_stats["wins"] / trade_stats["trades"] * 100

combined = monthly[["return_%", "last"]].rename(columns={"last":"eop_equity"})
combined = combined.join(trade_stats, how="left").fillna(0)
print("MONTHLY PERFORMANCE")
print("-" * 74)
print(f"{'Month':<10}{'Trades':>8}{'Wins':>8}{'WR %':>10}{'Return %':>14}{'Equity':>14}")
print("-" * 74)
for m, row in combined.iterrows():
    print(f"{str(m):<10}{int(row['trades']):>8}{int(row['wins']):>8}"
          f"{row['wr_%']:>10.2f}{row['return_%']:>+14.2f}${row['eop_equity']:>12.2f}")
print("-" * 74)

eq.to_csv(ROOT / "equity_curve_v2.csv")
