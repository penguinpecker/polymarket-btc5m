"""
FINAL backtest — same signal (contrarian / mean-reversion) with realistic
constraints:

  - Liquidity cap: max $2,000 per trade (Polymarket 5m markets are thin).
  - Conservative sizing: 2%/1.5%/1% of bankroll for tier A/B/C.
  - Three pricing scenarios:
      optimistic:  entry 0.485 + 0.5c slippage = 0.49
      base:        entry 0.500 + 0.5c slippage = 0.505
      pessimistic: entry 0.515 + 0.5c slippage = 0.52

  Same signal in all three → shows how sensitive P&L is to the entry price
  assumption (which we can't truly know without Polymarket tick archives).
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
    if row["hour"] in LOW_LIQ_HOURS: return (None, 0.0)
    if abs(row["vol_z"]) > 4:        return (None, 0.0)
    z = row["z_prev"]
    if z >  3.0:   return (-1, 0.020)       # tier A
    if z < -3.0:   return (+1, 0.020)
    if z >  2.0:   return (-1, 0.015)       # tier B
    if z < -2.0:   return (+1, 0.015)
    if row["ret_12"] >  0.005 and row["br_1"] > 0.55: return (-1, 0.010)  # C
    if row["ret_12"] < -0.005 and row["br_1"] < 0.45: return (+1, 0.010)
    return (None, 0.0)

tiers = df.apply(classify, axis=1)
df["direction"] = tiers.apply(lambda t: t[0])
df["size_frac"] = tiers.apply(lambda t: t[1])
df["trade"]     = df["direction"].notna()

MAX_STAKE = 2000.0
START_BANKROLL = 100.0

def run(entry_price, label):
    bankroll = START_BANKROLL
    equity_curve = []
    wins = losses = trades = 0
    tier_stats = {"A":[0,0,0], "B":[0,0,0], "C":[0,0,0]}

    for _, row in df.iterrows():
        if not row["trade"]:
            equity_curve.append((row["open_time"], bankroll))
            continue
        stake = min(bankroll * row["size_frac"], MAX_STAKE)
        sf = row["size_frac"]
        tier = "A" if sf == 0.020 else ("B" if sf == 0.015 else "C")
        tier_stats[tier][0] += 1
        tier_stats[tier][2] += stake
        if int(row["direction"]) == int(row["y"]):
            pnl = stake * (1.0 / entry_price - 1.0)
            wins += 1
            tier_stats[tier][1] += 1
        else:
            pnl = -stake
            losses += 1
        trades += 1
        bankroll += pnl
        equity_curve.append((row["open_time"], bankroll))

    eq = pd.DataFrame(equity_curve, columns=["ts","equity"]).set_index("ts")
    eq["drawdown"] = eq["equity"] / eq["equity"].cummax() - 1.0

    print("=" * 78)
    print(f"SCENARIO: {label}   (entry price = {entry_price:.3f}, "
          f"break-even WR = {entry_price:.1%})")
    print("=" * 78)
    print(f"Trades: {trades:,}   WR: {wins/trades:.3%}   "
          f"Edge: {(wins/trades/entry_price - 1)*100:+.2f}% per trade")
    for t in ("A","B","C"):
        n, w, s = tier_stats[t]
        if n:
            rule = {"A":"|z|>3","B":"|z|>2","C":"trend+tbd"}[t]
            print(f"  Tier {t} ({rule:9s}): {n:>5} trades, "
                  f"WR {w/n:.2%}, avg stake ${s/n:.2f}")
    print(f"Ending bankroll : ${bankroll:,.2f}")
    print(f"Total return    : {(bankroll/START_BANKROLL-1)*100:+,.2f}%")
    print(f"Max drawdown    : {eq['drawdown'].min()*100:.2f}%")

    # Monthly table
    eq2 = eq.copy()
    eq2.index = pd.to_datetime(eq2.index).tz_convert("UTC").tz_localize(None)
    eq2["month"] = eq2.index.to_period("M")
    monthly = eq2.groupby("month")["equity"].agg(["first","last"])
    monthly["return_%"] = (monthly["last"]/monthly["first"] - 1)*100

    df2 = df.copy()
    df2["month"] = df2["open_time"].dt.tz_convert("UTC").dt.tz_localize(None).dt.to_period("M")
    df2["win"] = df2["trade"] & (df2["direction"] == df2["y"])
    ts = df2[df2["trade"]].groupby("month").agg(
        trades=("trade","sum"), wins=("win","sum"))
    ts["wr_%"] = ts["wins"]/ts["trades"]*100
    combo = monthly[["return_%","last"]].rename(columns={"last":"equity"}).join(ts)
    combo = combo.fillna(0)

    print(f"\n{'Month':<10}{'Trades':>8}{'Wins':>7}{'WR %':>9}"
          f"{'Return %':>14}{'Equity $':>14}{'DD in month':>14}")
    print("-"*78)
    # monthly DD
    eq2["month_str"] = eq2["month"].astype(str)
    for m, row in combo.iterrows():
        m_mask = eq2["month_str"] == str(m)
        m_eq = eq2.loc[m_mask,"equity"]
        if len(m_eq):
            m_dd = (m_eq / m_eq.cummax() - 1).min() * 100
        else:
            m_dd = 0
        print(f"{str(m):<10}{int(row['trades']):>8}{int(row['wins']):>7}"
              f"{row['wr_%']:>9.2f}{row['return_%']:>+14.2f}"
              f"{row['equity']:>14,.2f}{m_dd:>13.2f}%")
    print()

run(0.490, "OPTIMISTIC  — contrarian discount 0.485 + 0.5c slip")
run(0.505, "BASE        — fair 0.500 + 0.5c slip (market knows about reversion)")
run(0.520, "PESSIMISTIC — reversion premium 0.515 + 0.5c slip")
