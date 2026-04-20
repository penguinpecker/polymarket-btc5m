"""Rich status reader for the paper bot."""
import json, pathlib, csv
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).parent / "paper"
s = json.loads((ROOT / "state.json").read_text())

start  = datetime.fromisoformat(s["start_iso"])
now    = datetime.now(timezone.utc)
days   = (now - start).total_seconds() / 86400
ret    = (s["bankroll"] / s["start_bankroll"] - 1) * 100
wr     = s["wins"] / max(s["trades"],1) * 100

print(f"  Started         : {start.isoformat(timespec='seconds')}")
print(f"  Running for     : {days:.2f} days")
print(f"  Start bankroll  : ${s['start_bankroll']:.2f}")
print(f"  Current bankroll: ${s['bankroll']:.2f}")
print(f"  Total return    : {ret:+.2f}%")
print(f"  Peak bankroll   : ${s['peak']:.2f}")
print(f"  Max drawdown    : {s['max_dd_pct']:.2f}%")
print(f"  Trades          : {s['trades']}   (wins {s['wins']} / losses {s['losses']})")
print(f"  Win rate        : {wr:.2f}%")
print()
print(f"  Polymarket live : {s['polymarket_reachable']}")
print(f"  Latency (median): {s.get('latency_ms_median')} ms")
print(f"  Basis flips     : {s.get('basis_flips', 0)}  (Chainlink ≠ Binance outcome)")
print(f"  Partial fills   : {s.get('partial_fills', 0)}")
print(f"  Skipped (spread): {s.get('skipped_spread', 0)}")
print(f"  Skipped (thin)  : {s.get('skipped_thin', 0)}")
print(f"  Avg fill ratio  : {s.get('avg_fill_ratio', 1.0):.3f}")
print(f"  Avg slip vs top : {s.get('avg_slippage_bps_vs_top', 0):+.1f} bps")
if s.get("open_trade"):
    t = s["open_trade"]
    print(f"\n  OPEN trade:")
    print(f"    tier     : {t['tier']}  dir={t['direction']:+}")
    print(f"    intended : ${t['stake_intended']:.3f}   filled: ${t['stake_filled']:.3f}")
    print(f"    vwap     : {t['vwap']:.4f}   top_ask={t.get('best_ask',0):.3f}  spread={t.get('spread',0):.3f}")
    print(f"    depth    : ${t.get('book_depth_usd',0):.0f}  levels={t.get('levels_walked',0)}  lat={t.get('latency_ms',0)}ms")

tf = ROOT / "trades.csv"
if tf.exists():
    rows = list(csv.DictReader(open(tf)))
    if rows:
        print(f"\n  Last 10 trades:")
        for r in rows[-10:]:
            outcome = int(r['chainlink_outcome'])
            flip = " FLIP" if int(r['basis_flip']) else ""
            tag = "WIN " if float(r["pnl"])>0 else ("TIE " if outcome==0 else "LOSS")
            print(f"    {r['open_time_utc'][:16]}  {r['tier']:18s}  "
                  f"dir={r['direction']:>+2}  vwap={float(r['vwap']):.3f}  "
                  f"fill={float(r['filled_ratio']):.2f}  "
                  f"bin={r['binance_outcome']:>+2} chain={r['chainlink_outcome']:>+2}{flip}  "
                  f"{tag} pnl={float(r['pnl']):+.3f}  bank=${float(r['bankroll_after']):.2f}")
