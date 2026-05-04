"""Ad-hoc reconcile: compare bot state.json bankroll to on-chain truth.

Usage:
  python3 reconcile.py             # check + report drift, no writes
  python3 reconcile.py --apply     # rewrite state.json bankroll/peak/
                                   # start_bankroll to chain_usd + locked
  python3 reconcile.py --set-start # only correct start_bankroll so the
                                   # cumulative return matches reality;
                                   # leaves bankroll at chain+locked
"""
import os
import sys
import json
import pathlib
from datetime import datetime, timezone

ROOT = pathlib.Path(os.environ.get("LIVE_DATA_DIR", "/app/live"))
STATE     = ROOT / "state.json"
POSITIONS = ROOT / "positions.json"

POLYGON_RPC = os.environ.get("POLYGON_RPC", "https://polygon-rpc.com")
LIVE_FUNDER = os.environ.get("LIVE_FUNDER", "")
PRIVATE_KEY = os.environ.get("LIVE_PRIVATE_KEY", "")


def main() -> int:
    apply_full = "--apply" in sys.argv
    set_start  = "--set-start" in sys.argv

    if not STATE.exists():
        print(f"no state file at {STATE}", file=sys.stderr)
        return 2
    s = json.loads(STATE.read_text())
    positions = json.loads(POSITIONS.read_text()) if POSITIONS.exists() else []

    OPEN = {"filled", "resolved"}
    locked = sum(float(p.get("stake_filled", 0.0)) for p in positions
                 if p.get("status") in OPEN)

    from pm_chain import make_w3, load_account, tradeable_collateral
    w3 = make_w3(POLYGON_RPC)
    if LIVE_FUNDER:
        owner = LIVE_FUNDER
    else:
        owner = load_account(PRIVATE_KEY).address
    chain_usd = tradeable_collateral(w3, owner)

    expected = chain_usd + locked
    drift    = round(s["bankroll"] - expected, 4)
    return_pct = (s["bankroll"] / s["start_bankroll"] - 1.0) * 100 if s["start_bankroll"] else 0

    print(f"  owner            : {owner}")
    print(f"  chain collateral : ${chain_usd:.4f}  (pUSD + USDC.e)")
    print(f"  open locked      : ${locked:.4f}  ({sum(1 for p in positions if p.get('status') in OPEN)} open)")
    print(f"  expected bankroll: ${expected:.4f}")
    print(f"  bot bankroll     : ${s['bankroll']:.4f}")
    print(f"  drift (bot-real) : ${drift:+.4f}")
    print(f"  start_bankroll   : ${s['start_bankroll']:.4f}")
    print(f"  trades / wins    : {s.get('trades',0)} / {s.get('wins',0)}")
    print(f"  current return   : {return_pct:+.2f}%")

    if apply_full:
        s["bankroll"] = round(expected, 4)
        s["peak"] = max(round(s.get("peak", 0), 4), s["bankroll"])
        s["start_bankroll"] = s["bankroll"] - sum(
            float(p.get("realized_pnl", 0.0)) for p in positions
            if p.get("status") not in OPEN
        )
        s["start_bankroll"] = round(s["start_bankroll"], 4)
        STATE.write_text(json.dumps(s, indent=2))
        print(f"  APPLIED          : bankroll=${s['bankroll']:.4f}  "
              f"start=${s['start_bankroll']:.4f}  peak=${s['peak']:.4f}")
    elif set_start:
        # Keep bankroll at chain+locked, recompute start so the cumulative
        # PnL implied by trades.csv matches the on-chain reality.
        realized_sum = sum(float(p.get("realized_pnl", 0.0)) for p in positions
                           if p.get("status") not in OPEN)
        s["start_bankroll"] = round(expected - realized_sum, 4)
        s["bankroll"] = round(expected, 4)
        s["peak"] = max(round(s.get("peak", 0), 4), s["bankroll"])
        STATE.write_text(json.dumps(s, indent=2))
        print(f"  APPLIED          : start=${s['start_bankroll']:.4f}  "
              f"bankroll=${s['bankroll']:.4f}")
    else:
        print(f"  (use --apply to overwrite bankroll, --set-start to fix only start_bankroll)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
