"""
Collect REAL Polymarket 5m BTC market history.

Run this from a non-US IP (VPN or non-US VPS) — Polymarket is geo-blocked
in the US. From an EU/ASIA VPS it runs clean.

Strategy:
  1. List every 5m market slug since launch (Feb 2026) via Gamma API.
  2. For each market, extract condition_id + two token_ids (UP / DOWN).
  3. Hit Data API /trades for every historical fill in that market.
  4. Persist per-market CSVs + a master index; resume on restart.

Trade-level data survives longer than the aggregated /prices-history
endpoint (which returns empty for resolved 5m markets — see py-clob-client
issue #216). From trades we reconstruct the within-window VWAP, last-trade
price, and approximate bid/ask path during each 5m window.

Run:
  python collect_polymarket.py --start 2026-02-01 --end 2026-04-20
  # resumes automatically from ./pm_data/_progress.json
"""
import argparse, json, pathlib, time, csv
from datetime import datetime, timezone, timedelta
import requests

GAMMA = "https://gamma-api.polymarket.com"
DATA  = "https://data-api.polymarket.com"
ROOT  = pathlib.Path(__file__).parent / "pm_data"
ROOT.mkdir(exist_ok=True)
PROGRESS = ROOT / "_progress.json"
MARKETS  = ROOT / "markets_index.csv"
TRADES_DIR = ROOT / "trades"
TRADES_DIR.mkdir(exist_ok=True)

SES = requests.Session()
SES.headers.update({"User-Agent": "polymarket-research/1.0"})

def floor_5m(dt: datetime) -> datetime:
    m = dt.minute - (dt.minute % 5)
    return dt.replace(minute=m, second=0, microsecond=0)

def iter_5m_timestamps(start: datetime, end: datetime):
    t = floor_5m(start)
    while t < end:
        yield int(t.timestamp())
        t += timedelta(minutes=5)

def load_progress():
    if PROGRESS.exists():
        return json.loads(PROGRESS.read_text())
    return {"completed_slugs": []}

def save_progress(p):
    PROGRESS.write_text(json.dumps(p))

def get_market(slug: str):
    """Gamma API — returns market metadata with condition_id and token_ids."""
    r = SES.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=15)
    if r.status_code != 200: return None
    data = r.json()
    if not data: return None
    m = data[0] if isinstance(data, list) else data
    # Token IDs live in the "clobTokenIds" field as a JSON-encoded list string
    token_ids = m.get("clobTokenIds")
    if isinstance(token_ids, str):
        token_ids = json.loads(token_ids)
    outcomes = m.get("outcomes")
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    return {
        "slug": slug,
        "condition_id": m.get("conditionId"),
        "question": m.get("question"),
        "start_date": m.get("startDate"),
        "end_date": m.get("endDate"),
        "closed": m.get("closed"),
        "resolved_outcome": m.get("umaResolutionStatus") or m.get("outcomePrices"),
        "outcome_prices": m.get("outcomePrices"),
        "token_up": token_ids[0] if token_ids else None,
        "token_down": token_ids[1] if token_ids and len(token_ids) > 1 else None,
        "volume": m.get("volume"),
        "liquidity": m.get("liquidity"),
    }

def get_trades(condition_id: str, start_ts: int, end_ts: int):
    """Data API — historical fills for a market. Paginate if needed."""
    out = []
    offset = 0
    while True:
        r = SES.get(f"{DATA}/trades", params={
            "market": condition_id,
            "start": start_ts,
            "end": end_ts,
            "limit": 500,
            "offset": offset,
            "sortBy": "TIMESTAMP",
            "sortDirection": "ASC",
        }, timeout=20)
        if r.status_code != 200:
            time.sleep(2.0)
            return out
        batch = r.json()
        if not batch: break
        out.extend(batch)
        if len(batch) < 500: break
        offset += 500
        time.sleep(0.1)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-02-01")
    ap.add_argument("--end",   default=datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"))
    ap.add_argument("--limit", type=int, default=0, help="max markets (0 = all)")
    ap.add_argument("--sleep", type=float, default=0.12, help="sec between API calls")
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end   = datetime.strptime(args.end,   "%Y-%m-%d").replace(tzinfo=timezone.utc)

    progress = load_progress()
    done = set(progress["completed_slugs"])

    new_index_mode = not MARKETS.exists()
    idx_f = open(MARKETS, "a", newline="")
    idx_w = csv.writer(idx_f)
    if new_index_mode:
        idx_w.writerow(["slug","condition_id","start_date","end_date","closed",
                        "outcome_prices","token_up","token_down","volume","n_trades"])

    count = 0
    for ts in iter_5m_timestamps(start, end):
        slug = f"btc-updown-5m-{ts}"
        if slug in done: continue
        if args.limit and count >= args.limit: break

        try:
            m = get_market(slug)
            time.sleep(args.sleep)
            if not m or not m["condition_id"]:
                done.add(slug)
                continue
            trades = get_trades(m["condition_id"], ts, ts + 300 + 120)
            time.sleep(args.sleep)

            tfile = TRADES_DIR / f"{slug}.csv"
            with open(tfile, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["timestamp","price","size","side","outcome","asset","taker"])
                for t in trades:
                    w.writerow([
                        t.get("timestamp"), t.get("price"), t.get("size"),
                        t.get("side"), t.get("outcome"), t.get("asset"),
                        t.get("proxyWallet"),
                    ])

            idx_w.writerow([
                slug, m["condition_id"], m["start_date"], m["end_date"],
                m["closed"], m["outcome_prices"], m["token_up"], m["token_down"],
                m["volume"], len(trades),
            ])
            idx_f.flush()
            done.add(slug)
            count += 1
            if count % 25 == 0:
                progress["completed_slugs"] = list(done)
                save_progress(progress)
                print(f"  [{count}] {slug}  {len(trades)} trades  vol={m['volume']}", flush=True)

        except requests.RequestException as e:
            print(f"  net error on {slug}: {e}", flush=True)
            time.sleep(5.0)

    progress["completed_slugs"] = list(done)
    save_progress(progress)
    idx_f.close()
    print(f"DONE. {count} new markets collected. Index: {MARKETS}")

if __name__ == "__main__":
    main()
