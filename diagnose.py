"""
Diagnose whether any feature derived from 5m OHLCV carries real predictive
power on next-bar direction. We measure raw win rate before fees — if we
can't clear 51% pre-fee on any slice, the strategy fundamentally cannot
work on OHLCV data alone.
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
df["ret_3"]     = df["close"].shift(1) / df["close"].shift(4) - 1.0
df["ret_6"]     = df["close"].shift(1) / df["close"].shift(7) - 1.0
df["ret_12"]    = df["close"].shift(1) / df["close"].shift(13) - 1.0
df["br_1"]      = df["buy_ratio"].shift(1)
df["br_3"]      = df["buy_ratio"].shift(1).rolling(3).mean()
df["sigma"]     = df["ret"].shift(1).rolling(24).std()
df["z_prev"]    = df["ret_1"] / df["sigma"]
df = df.dropna().reset_index(drop=True)

def wr(mask, direction):
    sub = df[mask]
    if len(sub) < 30: return None
    wins = (np.sign(direction[mask]) == sub["y"]).mean()
    return wins, len(sub)

print("=" * 72)
print("FEATURE-BY-FEATURE WIN RATE (predicting next-bar direction, pre-fees)")
print("Break-even after 2.5c spread ≈ 52.5%.  Random = 50%.")
print("=" * 72)

tests = [
    ("momentum: last-bar direction",       np.sign(df["ret_1"])),
    ("mean-revert: opposite last-bar",    -np.sign(df["ret_1"])),
    ("3-bar momentum",                      np.sign(df["ret_3"])),
    ("3-bar mean-revert",                  -np.sign(df["ret_3"])),
    ("12-bar momentum (1h)",                np.sign(df["ret_12"])),
    ("taker-buy ratio > 0.5",               np.where(df["br_1"]>0.5, 1, -1)),
    ("taker-buy ratio < 0.5 (contrarian)",  np.where(df["br_1"]>0.5, -1, 1)),
    ("taker-buy 3-bar avg > 0.5",           np.where(df["br_3"]>0.5, 1, -1)),
    ("extreme move reversion (|z|>2)",     -np.sign(df["ret_1"])),
    ("extreme move continuation (|z|>2)",   np.sign(df["ret_1"])),
]

print(f"\n{'Signal':<42}{'n':>8}{'WR':>10}{'edge bps':>12}")
print("-" * 72)
for name, direction in tests:
    if "extreme" in name:
        mask = df["z_prev"].abs() > 2.0
    else:
        mask = np.ones(len(df), dtype=bool)
    mask = mask & np.isfinite(df["sigma"])
    wins = (np.sign(direction[mask]) == df["y"][mask]).mean()
    n = mask.sum()
    print(f"{name:<42}{n:>8,}{wins*100:>9.2f}%{(wins-0.5)*10000:>10.1f}")

print("\n" + "=" * 72)
print("STRONGEST SINGLE FEATURE BY DECILE (3-bar taker-buy average)")
print("=" * 72)
df["br3_decile"] = pd.qcut(df["br_3"], 10, labels=False, duplicates="drop")
dec = df.groupby("br3_decile").agg(
    n=("y", "size"),
    up_rate=("y", lambda s: (s > 0).mean()),
    mean_br3=("br_3", "mean"),
)
print(dec)

print("\n" + "=" * 72)
print("BY HOUR: up-rate conditional on taker-buy >0.52 in prior bar")
print("=" * 72)
strong = df[df["br_1"] > 0.52]
by_hour = strong.groupby("hour").agg(
    n=("y","size"),
    up=("y", lambda s: (s>0).mean())
)
print(by_hour)

print("\n" + "=" * 72)
print("EXTREME signals (highest conviction only)")
print("=" * 72)
extreme_cases = [
    ("br_1 > 0.60",                df["br_1"] > 0.60,   1),
    ("br_1 < 0.40",                df["br_1"] < 0.40,  -1),
    ("br_3 > 0.56",                df["br_3"] > 0.56,   1),
    ("br_3 < 0.44",                df["br_3"] < 0.44,  -1),
    ("z_prev>3 -> revert",         df["z_prev"] > 3,   -1),
    ("z_prev<-3 -> revert",        df["z_prev"] < -3,   1),
    ("1h trend up + br_1>0.55",   (df["ret_12"] > 0.005) & (df["br_1"] > 0.55), 1),
    ("1h trend dn + br_1<0.45",   (df["ret_12"] < -0.005) & (df["br_1"] < 0.45), -1),
]
print(f"{'Rule':<34}{'n':>8}{'WR':>10}{'edge bps':>12}")
for name, mask, d in extreme_cases:
    mm = mask.fillna(False)
    if mm.sum() < 30:
        print(f"{name:<34}{mm.sum():>8}{'n/a':>10}")
        continue
    wr = (df["y"][mm] == d).mean()
    print(f"{name:<34}{mm.sum():>8}{wr*100:>9.2f}%{(wr-0.5)*10000:>10.1f}")
