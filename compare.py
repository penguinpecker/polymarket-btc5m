"""Side-by-side compare of both bots."""
import json, pathlib, csv
from datetime import datetime, timezone

def load(dirname):
    p = pathlib.Path(__file__).parent / dirname
    if not (p / "state.json").exists(): return None
    s = json.loads((p / "state.json").read_text())
    trades = []
    tf = p / "trades.csv"
    if tf.exists():
        trades = list(csv.DictReader(open(tf)))
    return s, trades

def summarize(name, s, trades):
    if not s: return f"{name:12s}  (no state yet)"
    wr = s["wins"] / max(s["trades"],1) * 100
    ret = (s["bankroll"] / s["start_bankroll"] - 1) * 100
    exit_counts = {}
    for r in trades:
        et = r.get("exit_type", "expiry") or "expiry"
        exit_counts[et] = exit_counts.get(et, 0) + 1
    exits = ", ".join(f"{k}={v}" for k, v in exit_counts.items()) or "—"
    return (f"{name:12s}  ${s['bankroll']:>8.2f}  "
            f"ret {ret:+7.2f}%   WR {wr:>5.1f}%   "
            f"trades={s['trades']:>3}  DD {s['max_dd_pct']:>5.2f}%   "
            f"exits=[{exits}]")

a = load("paper")
b = load("paper_exit")
print(f"{'':12s}  {'bankroll':>9s}  {'return':>9s}   {'WR':>7s}   trades  {'DD':>9s}   exits")
print("-" * 110)
if a: print(summarize("hold-expiry", *a))
if b: print(summarize("SL/TP exit",  *b))
