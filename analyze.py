"""
Trade-level analytics for the paper bot.
Run anytime: `python analyze.py`  — reads paper/trades.csv, prints:

  * Overall headline stats
  * Per-tier breakdown (A / B / C)
  * Per-direction (UP / DOWN) win rate
  * Entry-price buckets (0.0-0.2, 0.2-0.4, ...) with WR and EV
  * Basis-flip rate (Chainlink ≠ Binance)
  * Resolution source distribution
  * Latency stats
  * Cumulative P&L, max DD recomputed from scratch

Use this to decide when to add per-tier entry-price filters.
"""
import csv, pathlib, statistics, json
from collections import defaultdict

ROOT = pathlib.Path(__file__).parent / "paper"
TF   = ROOT / "trades.csv"
SF   = ROOT / "state.json"

if not TF.exists() or TF.stat().st_size < 100:
    print("No trades yet."); raise SystemExit(0)

rows = list(csv.DictReader(open(TF)))
if not rows:
    print("No trades yet."); raise SystemExit(0)

for r in rows:
    for k in ("stake_intended","stake_filled","vwap","best_ask","best_bid",
              "spread","book_depth_usd","filled_ratio","pnl","bankroll_after","latency_ms"):
        r[k] = float(r[k])
    r["direction"]         = int(r["direction"])
    r["levels_walked"]     = int(r["levels_walked"])
    r["binance_outcome"]   = int(r["binance_outcome"])
    r["chainlink_outcome"] = int(r["chainlink_outcome"])
    r["basis_flip"]        = int(r["basis_flip"])
    r["tier_letter"]       = r["tier"][0]  # A / B / C
    r["is_win"]            = r["pnl"] > 0

n = len(rows)
wins = sum(1 for r in rows if r["is_win"])
losses = n - wins
wr = wins / n * 100
starting = 100.0
ending   = rows[-1]["bankroll_after"]
total_ret = (ending/starting - 1) * 100
stakes   = [r["stake_filled"] for r in rows]
pnls     = [r["pnl"] for r in rows]
lat      = [r["latency_ms"] for r in rows]
flips    = sum(r["basis_flip"] for r in rows)

print("=" * 78)
print(f"{'HEADLINE':<78}")
print("=" * 78)
print(f"  trades        : {n}")
print(f"  wins / losses : {wins} / {losses}")
print(f"  win rate      : {wr:.2f}%")
print(f"  total return  : {total_ret:+.2f}%   (${starting:.2f} → ${ending:.2f})")
print(f"  avg stake     : ${statistics.mean(stakes):.3f}   median: ${statistics.median(stakes):.3f}")
print(f"  avg pnl       : ${statistics.mean(pnls):+.4f}    total: ${sum(pnls):+.4f}")
print(f"  basis flips   : {flips} / {n} ({flips/n*100:.1f}%)")
print(f"  latency (ms)  : median {statistics.median(lat):.0f}  "
      f"min {min(lat):.0f}  max {max(lat):.0f}")

print("\n" + "=" * 78)
print(f"{'PER-TIER BREAKDOWN':<78}")
print("=" * 78)
by_tier = defaultdict(list)
for r in rows: by_tier[r["tier_letter"]].append(r)
hist_wr = {"A": 58.7, "B": 54.2, "C": 55.7}  # from backtest_final.py output
print(f"{'tier':<6}{'n':>4}{'wins':>6}{'WR %':>8}{'hist':>8}{'avg_vwap':>10}{'avg_pnl':>10}{'tot_pnl':>10}")
for t in ("A","B","C"):
    rs = by_tier[t]
    if not rs:
        print(f"  {t:<4}{'-':>4}"); continue
    n_t = len(rs); w_t = sum(1 for r in rs if r["is_win"])
    avg_vwap = statistics.mean(r["vwap"] for r in rs)
    avg_pnl  = statistics.mean(r["pnl"] for r in rs)
    tot_pnl  = sum(r["pnl"] for r in rs)
    print(f"  {t:<4}{n_t:>4}{w_t:>6}{w_t/n_t*100:>7.1f}%{hist_wr[t]:>7.1f}%"
          f"{avg_vwap:>10.3f}{avg_pnl:>+10.3f}{tot_pnl:>+10.3f}")

print("\n" + "=" * 78)
print(f"{'PER-DIRECTION':<78}")
print("=" * 78)
for d, name in [(1, "UP"), (-1, "DOWN")]:
    rs = [r for r in rows if r["direction"] == d]
    if not rs: continue
    n_d = len(rs); w_d = sum(1 for r in rs if r["is_win"])
    tot = sum(r["pnl"] for r in rs)
    print(f"  {name:<6}  n={n_d}  wins={w_d}  WR={w_d/n_d*100:.1f}%  total_pnl={tot:+.3f}")

print("\n" + "=" * 78)
print(f"{'ENTRY-PRICE BUCKETS (vwap)':<78}")
print("=" * 78)
buckets = [(0.0,0.2),(0.2,0.35),(0.35,0.5),(0.5,0.65),(0.65,0.8),(0.8,1.0)]
print(f"{'bucket':<14}{'n':>4}{'wins':>6}{'WR %':>8}{'break_even_WR':>15}{'avg_pnl':>10}")
for lo, hi in buckets:
    rs = [r for r in rows if lo <= r["vwap"] < hi]
    if not rs: continue
    n_b = len(rs); w_b = sum(1 for r in rs if r["is_win"])
    avg_pnl = statistics.mean(r["pnl"] for r in rs)
    mid = (lo+hi)/2
    print(f"  [{lo:.2f},{hi:.2f})  {n_b:>3}{w_b:>6}{w_b/n_b*100:>7.1f}%{mid*100:>14.1f}%{avg_pnl:>+10.3f}")

print("\n" + "=" * 78)
print(f"{'RESOLUTION SOURCE':<78}")
print("=" * 78)
fill_src = defaultdict(int)
res_src  = defaultdict(int)
for r in rows:
    fill_src[r.get("fill_source","?")] += 1
    res_src[r.get("resolve_source","?")] += 1
print("  entry fill source:")
for k, c in sorted(fill_src.items(), key=lambda x: -x[1]):
    print(f"    {k:<30} {c}")
print("  resolution source:")
for k, c in sorted(res_src.items(), key=lambda x: -x[1]):
    print(f"    {k:<30} {c}")

print("\n" + "=" * 78)
print(f"{'EQUITY / DRAWDOWN':<78}")
print("=" * 78)
eq = [starting]
for r in rows: eq.append(r["bankroll_after"])
peak = eq[0]; max_dd = 0
for x in eq:
    peak = max(peak, x)
    max_dd = max(max_dd, (peak - x)/peak * 100)
print(f"  max drawdown : {max_dd:.2f}%")
print(f"  peak equity  : ${max(eq):.3f}")
print(f"  trough equity: ${min(eq):.3f}")

print("\n" + "=" * 78)
print(f"{'TIER B vs TIER C at-a-glance':<78}")
print("=" * 78)
for t in ("B","C"):
    rs = by_tier[t]
    if not rs: continue
    vs = [r["vwap"] for r in rs]
    print(f"  Tier {t} entries: min={min(vs):.3f}  max={max(vs):.3f}  "
          f"median={statistics.median(vs):.3f}  n={len(rs)}")
