"""
Download 6 months of BTCUSDT 5m klines from Binance.
Binance is the largest single feed Chainlink's BTC/USD stream aggregates,
so its 5m OHLC tracks Chainlink's window-start/window-end prices within a few bps.
"""
import time, json, pathlib, requests
import pandas as pd
from datetime import datetime, timezone

OUT = pathlib.Path(__file__).parent / "btcusdt_5m.csv"
END = int(datetime(2026, 4, 21, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
START = int(datetime(2025, 10, 21, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
URL = "https://api.binance.com/api/v3/klines"

def fetch(start_ms, end_ms):
    rows = []
    cur = start_ms
    while cur < end_ms:
        r = requests.get(URL, params={
            "symbol": "BTCUSDT", "interval": "5m",
            "startTime": cur, "endTime": end_ms, "limit": 1000
        }, timeout=15)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        cur = batch[-1][0] + 1
        time.sleep(0.15)
        print(f"  {datetime.fromtimestamp(cur/1000, tz=timezone.utc)} ({len(rows)} rows)", flush=True)
    return rows

print("Downloading BTCUSDT 5m klines...")
rows = fetch(START, END)
df = pd.DataFrame(rows, columns=[
    "open_time","open","high","low","close","volume",
    "close_time","quote_volume","trades","taker_buy_base","taker_buy_quote","ignore"
])
for c in ["open","high","low","close","volume","quote_volume","taker_buy_base","taker_buy_quote"]:
    df[c] = df[c].astype(float)
df["trades"] = df["trades"].astype(int)
df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
df = df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
df.to_csv(OUT, index=False)
print(f"Saved {len(df)} rows to {OUT}")
print(f"Range: {df['open_time'].min()} -> {df['open_time'].max()}")
