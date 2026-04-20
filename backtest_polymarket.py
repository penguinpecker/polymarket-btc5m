"""
Backtest the contrarian 5m strategy against REAL Polymarket fill data.

Inputs (whichever exists):
  ./pm_data/markets_index.csv  +  ./pm_data/trades/*.csv    (from collect_polymarket.py)
  ./pm_data/dune_trades.csv                                  (from dune_query.sql)
  ./btcusdt_5m.csv                                           (Binance — for signal)

Simulation is faithful to a real Polymarket executor:
  1. Signal fires at the OPEN of each 5m window using prior Binance bars.
  2. If trade triggered, we wait T_ENTRY seconds into the window (default
     30s) to observe the Polymarket book. We take LIQUIDITY on our side at
     the current best-ask on the contrarian outcome.
  3. Market resolves at +300s based on Chainlink; the market_index.csv
     tells us which outcome won (from outcomePrices).
  4. P&L = stake/ask - stake on win, -stake on loss. No trading fees
     (Polymarket CLOB has none), slippage is IMPLICIT in the real ask.
"""
import csv, json, pathlib, sys
import numpy as np
import pandas as pd
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).parent
PM   = ROOT / "pm_data"
IDX  = PM / "markets_index.csv"
TRD  = PM / "trades"
DUNE = PM / "dune_trades.csv"
BIN  = ROOT / "btcusdt_5m.csv"

if not BIN.exists():
    sys.exit("Run download.py first (Binance OHLC).")

# ---- 1. Signal: identical to backtest_final.py contrarian tiers ----
df = pd.read_csv(BIN, parse_dates=["open_time","close_time"]).sort_values("open_time").reset_index(drop=True)
df["ret"]       = df["close"]/df["open"] - 1
df["buy_ratio"] = df["taker_buy_base"]/df["volume"].replace(0,np.nan)
df["hour"]      = df["open_time"].dt.hour
df["ret_1"]     = df["ret"].shift(1)
df["ret_12"]    = df["close"].shift(1)/df["close"].shift(13) - 1
df["br_1"]      = df["buy_ratio"].shift(1)
df["vol_1"]     = df["volume"].shift(1)
df["sigma"]     = df["ret"].shift(1).rolling(24).std()
df["z_prev"]    = df["ret_1"]/df["sigma"]
df["vol_ma"]    = df["vol_1"].rolling(288).mean()
df["vol_sd"]    = df["vol_1"].rolling(288).std()
df["vol_z"]     = (df["vol_1"]-df["vol_ma"])/df["vol_sd"]
df = df.dropna().reset_index(drop=True)
df["bar_ts"]    = (df["open_time"].astype("int64")//10**9).astype(int)

def classify(r):
    if r["hour"] in (3,4,5): return (None, 0.0)
    if abs(r["vol_z"]) > 4:  return (None, 0.0)
    z = r["z_prev"]
    if z >  3:  return (-1, 0.020)
    if z < -3:  return (+1, 0.020)
    if z >  2:  return (-1, 0.015)
    if z < -2:  return (+1, 0.015)
    if r["ret_12"]>0.005 and r["br_1"]>0.55: return (-1, 0.010)
    if r["ret_12"]<-0.005 and r["br_1"]<0.45: return (+1, 0.010)
    return (None, 0.0)

tiers = df.apply(classify, axis=1)
df["direction"] = tiers.apply(lambda t: t[0])
df["size_frac"] = tiers.apply(lambda t: t[1])
df["trade"]     = df["direction"].notna()

# ---- 2. Load real Polymarket data ----
pm_index = None
pm_trades_by_slug = {}

if IDX.exists():
    pm_index = pd.read_csv(IDX)
    # outcome_prices is a JSON list string like '["1","0"]' (UP wins) or '["0","1"]' (DOWN wins)
    def parse_winner(s):
        try:
            arr = json.loads(s) if isinstance(s,str) else s
            if float(arr[0]) > 0.5: return +1   # UP won
            if float(arr[1]) > 0.5: return -1   # DOWN won
        except: pass
        return 0
    pm_index["winner"]   = pm_index["outcome_prices"].apply(parse_winner)
    pm_index["start_ts"] = pm_index["slug"].str.extract(r"btc-updown-5m-(\d+)").astype(int)
    print(f"[pm] loaded index: {len(pm_index)} markets, "
          f"{(pm_index['winner']!=0).sum()} resolved")

    for _, row in pm_index.iterrows():
        p = TRD / f"{row['slug']}.csv"
        if p.exists() and p.stat().st_size > 80:
            pm_trades_by_slug[int(row["start_ts"])] = p

elif DUNE.exists():
    dune = pd.read_csv(DUNE)
    dune["start_ts"] = dune["slug"].str.extract(r"btc-updown-5m-(\d+)").astype(int)
    for ts, g in dune.groupby("start_ts"):
        pm_trades_by_slug[int(ts)] = g
    print(f"[dune] loaded {len(pm_trades_by_slug)} markets")
else:
    print("[pm] no real data found — run collect_polymarket.py or dune_query.sql first.")
    print("     Falling through to synthetic pricing (same as backtest_final.py).")

# ---- 3. Executor: fill on the contrarian side at T_ENTRY seconds ----
T_ENTRY = 30           # wait 30s into the 5m window to read the book
MAX_STAKE = 2000.0
START = 100.0
FALLBACK_PRICE = 0.51  # if no Polymarket data, use base scenario from synthetic

def contrarian_token(direction, token_up_id, token_down_id):
    return token_up_id if direction == +1 else token_down_id

def resolve_fill_price(start_ts, direction, slug_row):
    """Return the best-ask on our side at start_ts + T_ENTRY from real trades."""
    if start_ts not in pm_trades_by_slug:
        return FALLBACK_PRICE, "fallback"
    src = pm_trades_by_slug[start_ts]
    if isinstance(src, pathlib.Path):
        t = pd.read_csv(src)
    else:
        t = src
    if t.empty: return FALLBACK_PRICE, "empty"

    # Pick the asset on our side (UP token id for direction=+1, else DOWN)
    target_asset = slug_row["token_up"] if direction == +1 else slug_row["token_down"]
    t["timestamp"] = pd.to_numeric(t["timestamp"], errors="coerce")
    t["price"]     = pd.to_numeric(t["price"],     errors="coerce")
    t_ours = t[t["asset"].astype(str) == str(target_asset)]
    # Take trades in the [start_ts + T_ENTRY - 10s, start_ts + T_ENTRY + 30s] window
    lo, hi = start_ts + T_ENTRY - 10, start_ts + T_ENTRY + 30
    window = t_ours[(t_ours["timestamp"] >= lo) & (t_ours["timestamp"] <= hi)]
    if window.empty:
        # fallback: most recent trade before our entry
        prev = t_ours[t_ours["timestamp"] <= start_ts + T_ENTRY]
        if prev.empty: return FALLBACK_PRICE, "no-prior"
        return float(prev.iloc[-1]["price"]), "last-before"
    # We buy (take ask) → use SELL-side trades' price (someone selling to us)
    asks = window[window["side"].str.upper() == "SELL"]
    if not asks.empty:
        return float(asks.iloc[0]["price"]), "real-ask"
    return float(window.iloc[0]["price"]), "any-trade"

# Build lookup for market winner by start_ts
winner_map = {}
meta_map = {}
if pm_index is not None:
    for _, row in pm_index.iterrows():
        winner_map[int(row["start_ts"])] = int(row["winner"])
        meta_map[int(row["start_ts"])]   = row

bankroll = START
eq = []
wins = losses = trades_taken = skipped_no_market = 0
fill_sources = {}

for _, bar in df.iterrows():
    if not bar["trade"]:
        eq.append((bar["open_time"], bankroll)); continue
    start_ts = int(bar["bar_ts"])
    direction = int(bar["direction"])

    # Truth from real Polymarket resolution if available, else Binance close:
    if start_ts in winner_map and winner_map[start_ts] != 0:
        outcome = winner_map[start_ts]
        slug_row = meta_map[start_ts]
        entry_price, src = resolve_fill_price(start_ts, direction, slug_row)
    else:
        outcome = +1 if (bar["close"] > bar["open"]) else -1
        entry_price, src = FALLBACK_PRICE, "synthetic"
        skipped_no_market += 1
    fill_sources[src] = fill_sources.get(src, 0) + 1

    # Guardrails
    if entry_price <= 0.01 or entry_price >= 0.99:
        eq.append((bar["open_time"], bankroll)); continue

    stake = min(bankroll * bar["size_frac"], MAX_STAKE)
    if direction == outcome:
        pnl = stake * (1/entry_price - 1); wins += 1
    else:
        pnl = -stake; losses += 1
    bankroll += pnl
    trades_taken += 1
    eq.append((bar["open_time"], bankroll))

eqdf = pd.DataFrame(eq, columns=["ts","equity"]).set_index("ts")
eqdf["dd"] = eqdf["equity"]/eqdf["equity"].cummax() - 1

print("=" * 74)
print("POLYMARKET BTC 5m — BACKTEST AGAINST REAL FILLS (where available)")
print("=" * 74)
print(f"Trades taken       : {trades_taken:,}")
print(f"  with real fill   : {trades_taken - skipped_no_market:,}")
print(f"  synthetic fill   : {skipped_no_market:,}")
print(f"Win rate           : {wins/max(trades_taken,1):.3%}")
print(f"Fill-source counts : {fill_sources}")
print(f"Starting $         : {START:,.2f}")
print(f"Ending   $         : {bankroll:,.2f}")
print(f"Total return       : {(bankroll/START-1)*100:+,.2f}%")
print(f"Max drawdown       : {eqdf['dd'].min()*100:.2f}%")

eqdf["month"] = pd.to_datetime(eqdf.index).tz_convert("UTC").tz_localize(None).to_period("M")
mo = eqdf.groupby("month")["equity"].agg(["first","last"])
mo["ret_%"] = (mo["last"]/mo["first"]-1)*100
print("\nMonthly:")
print(mo.to_string())

eqdf.to_csv(ROOT / "equity_curve_polymarket.csv")
