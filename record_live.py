"""
Record LIVE Polymarket 5m BTC market orderbook + trades in real time.

Run from a non-US IP. Dependencies: `pip install websockets requests`.

Loop:
  1. Every 5 minutes, query Gamma API for the 5m BTC market that just opened.
  2. Open a WSS subscription on its two token_ids (UP + DOWN).
  3. Log every book/price_change/last_trade message to CSV.
  4. At market close (start_ts + 300s), close the sub and roll to the next.

After 1–2 weeks you'll have the real fill-quality data needed to calibrate
the "optimistic / base / pessimistic" entry-price assumption in the
synthetic backtest.
"""
import asyncio, json, pathlib, time, csv
from datetime import datetime, timezone, timedelta
import requests
import websockets

GAMMA = "https://gamma-api.polymarket.com"
WSS   = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
ROOT  = pathlib.Path(__file__).parent / "pm_live"
ROOT.mkdir(exist_ok=True)

def floor_5m(dt):
    return dt.replace(minute=dt.minute - dt.minute%5, second=0, microsecond=0)

def get_market(ts):
    slug = f"btc-updown-5m-{ts}"
    r = requests.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=10)
    if r.status_code != 200 or not r.json(): return None
    m = r.json()[0]
    tokens = json.loads(m["clobTokenIds"]) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds")
    return {"slug": slug, "start_ts": ts, "token_up": tokens[0], "token_down": tokens[1],
            "condition_id": m.get("conditionId")}

async def record_window(market, duration_s=360):
    """Record one 5m window's full book/trade stream."""
    csv_path = ROOT / f"{market['slug']}.csv"
    f = open(csv_path, "w", newline="")
    w = csv.writer(f)
    w.writerow(["recv_ts","event","asset_id","best_bid","best_ask","mid",
                "last_price","size","side","raw_json"])

    async with websockets.connect(WSS, ping_interval=10, ping_timeout=20) as ws:
        sub = {"type":"MARKET",
               "assets_ids":[market["token_up"], market["token_down"]]}
        await ws.send(json.dumps(sub))
        end = time.time() + duration_s
        while time.time() < end:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            now = time.time()
            try:
                data = json.loads(msg)
            except:
                continue
            items = data if isinstance(data, list) else [data]
            for it in items:
                etype = it.get("event_type","")
                asset = it.get("asset_id","")
                bb = ba = mid = lp = sz = sd = ""
                if etype == "book":
                    bids = it.get("bids",[]); asks = it.get("asks",[])
                    bb = bids[-1]["price"] if bids else ""
                    ba = asks[0]["price"]  if asks else ""
                    if bb and ba:
                        mid = (float(bb)+float(ba))/2
                elif etype == "price_change":
                    changes = it.get("changes",[])
                    if changes:
                        bb = min((c["price"] for c in changes if c.get("side")=="BUY"), default="")
                        ba = min((c["price"] for c in changes if c.get("side")=="SELL"), default="")
                elif etype == "last_trade_price":
                    lp = it.get("price",""); sz = it.get("size",""); sd = it.get("side","")
                w.writerow([f"{now:.3f}", etype, asset, bb, ba, mid, lp, sz, sd, json.dumps(it)[:400]])
            f.flush()
    f.close()

async def main_loop():
    while True:
        now = datetime.now(timezone.utc)
        next_open = floor_5m(now) + timedelta(minutes=5)
        wait = (next_open - now).total_seconds() - 2
        if wait > 0:
            print(f"waiting {wait:.1f}s for next 5m open ({next_open.isoformat()})")
            await asyncio.sleep(wait)
        ts = int(next_open.timestamp())
        m = get_market(ts)
        if not m:
            print(f"  market btc-updown-5m-{ts} not found yet, retrying")
            await asyncio.sleep(5); continue
        print(f"recording {m['slug']}")
        try:
            await record_window(m, duration_s=360)
        except Exception as e:
            print(f"  error: {e}")
            await asyncio.sleep(3)

if __name__ == "__main__":
    asyncio.run(main_loop())
