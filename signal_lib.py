"""Signal + market-data library for the Polymarket BTC-UpDown-5m bot.

Pure library — no on-disk state, no trading loop. Imported by the live
trader and ops scripts. Importing also installs a DoH-based DNS bypass
for *.polymarket.com (Indian residential ISPs hijack the lookup).
"""
import json
import time
import socket
from datetime import datetime, timezone, timedelta

import requests

# ---------- ISP DNS bypass (India ISPs hijack polymarket.com) ----------
_DOH_CACHE = {}
_DOH_TTL   = 300


def _doh_resolve(host):
    now = time.time()
    hit = _DOH_CACHE.get(host)
    if hit and hit[1] > now:
        return hit[0]
    try:
        r = requests.get("https://cloudflare-dns.com/dns-query",
                         params={"name": host, "type": "A"},
                         headers={"accept": "application/dns-json"}, timeout=5)
        ips = [a["data"] for a in r.json().get("Answer", []) if a.get("type") == 1]
        if ips:
            _DOH_CACHE[host] = (ips, now + _DOH_TTL)
            return ips
    except Exception:
        pass
    return []


_orig_getaddrinfo = socket.getaddrinfo


def _patched_getaddrinfo(host, *a, **kw):
    if host and host.endswith("polymarket.com"):
        for ip in _doh_resolve(host):
            try:
                return _orig_getaddrinfo(ip, *a, **kw)
            except Exception:
                continue
    return _orig_getaddrinfo(host, *a, **kw)


socket.getaddrinfo = _patched_getaddrinfo

# ---------- constants ----------
BINANCE = "https://data-api.binance.vision/api/v3/klines"  # public CDN — works from US-region hosts
GAMMA   = "https://gamma-api.polymarket.com"
CLOB    = "https://clob.polymarket.com"

MAX_STAKE           = 2000.00
DEPTH_CAP_FRACTION  = 0.10      # max 10% of same-side book depth
MIN_DEPTH_USD       = 300.00    # skip if thinner than this
MAX_SPREAD          = 0.04      # skip if best_ask - best_bid > 4c (Tier C)
MAX_SPREAD_A        = 0.06      # Tier A has stronger signal — allow 6c
MAX_ENTRY_C         = 0.55      # Tier C: skip if entry > 0.55 (EV < 0 at 56% WR)
MAX_ENTRY_A         = 0.70      # Tier A: skip if entry > 0.70 (EV marginal at 59% WR)
GAS_COST_USD        = 0.01      # Polygon USDC tx cost (symbolic)
LOW_LIQ_HOURS       = {3, 4, 5}
T_ENTRY_OFFSET_S    = 30        # observe book this many sec into window
RESOLUTION_TIMEOUT  = 300       # seconds to wait for Polymarket resolution


# ---------- market data ----------
def fetch_recent_klines(n=60, cutoff_ms=None):
    """Return the last n fully-closed 5m bars.
    cutoff_ms: only keep bars with open_time < cutoff_ms. Defaults to
    (now_utc - 5min) so the currently-forming bar is excluded even if
    Binance hasn't yet emitted it (race we hit at boundary crossings)."""
    r = requests.get(BINANCE, params={
        "symbol": "BTCUSDT", "interval": "5m", "limit": n + 2
    }, timeout=10)
    r.raise_for_status()
    rows = r.json()
    if cutoff_ms is None:
        cutoff_ms = int(time.time() * 1000) - 5 * 60 * 1000 + 1
    rows = [k for k in rows if k[0] < cutoff_ms]
    rows = rows[-n:]
    return [{"open_time": k[0], "open": float(k[1]), "high": float(k[2]),
             "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
             "taker_buy_base": float(k[9])} for k in rows]


def get_market_by_slug(slug, include_closed=False):
    """Gamma /markets defaults to active-only; pass include_closed to reach
    resolved markets (required for reading outcomePrices post-settlement)."""
    try:
        params = {"slug": slug}
        if include_closed:
            params["closed"] = "true"
        r = requests.get(f"{GAMMA}/markets", params=params, timeout=5)
        if r.status_code != 200:
            return None
        js = r.json()
        if not js and not include_closed:
            params["closed"] = "true"
            r = requests.get(f"{GAMMA}/markets", params=params, timeout=5)
            js = r.json() if r.status_code == 200 else []
        if not js:
            return None
        m = js[0]
        tokens = m.get("clobTokenIds")
        if isinstance(tokens, str):
            tokens = json.loads(tokens)
        return {
            "slug": slug, "condition_id": m.get("conditionId"),
            "token_up":   tokens[0] if tokens else None,
            "token_down": tokens[1] if tokens and len(tokens) > 1 else None,
            "closed": bool(m.get("closed")),
            "outcome_prices": m.get("outcomePrices"),
        }
    except Exception:
        return None


def fetch_book(token_id):
    try:
        r = requests.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=5)
        if r.status_code != 200:
            return None
        b = r.json()
        bids = sorted([(float(x["price"]), float(x["size"])) for x in b.get("bids", [])], reverse=True)
        asks = sorted([(float(x["price"]), float(x["size"])) for x in b.get("asks", [])])
        return {"bids": bids, "asks": asks}
    except Exception:
        return None


def walk_ask_book(book, stake_usd, depth_cap_fraction=DEPTH_CAP_FRACTION):
    """Fill stake_usd of notional against the ask side, return fill dict or None."""
    if not book or not book["asks"]:
        return None
    asks = [(p, s) for (p, s) in book["asks"] if 0.01 < p < 0.99]
    if not asks:
        return None

    total_depth_usd = sum(p * s for p, s in asks)
    best_ask = asks[0][0]
    best_bid = book["bids"][0][0] if book["bids"] else 0.0
    spread   = best_ask - best_bid

    cap = total_depth_usd * depth_cap_fraction
    target = min(stake_usd, cap, MAX_STAKE)

    remaining = target
    cost_usd = 0.0
    shares   = 0.0
    levels   = 0
    last_price_hit = best_ask
    for price, size in asks:
        if remaining <= 0.01:
            break
        level_cap = price * size
        take = min(level_cap, remaining)
        cost_usd += take
        shares   += take / price
        remaining -= take
        levels   += 1
        last_price_hit = price

    if shares <= 0:
        return None
    vwap = cost_usd / shares
    filled_ratio = cost_usd / max(stake_usd, 1e-9)
    slippage_bps = (vwap - best_ask) * 10000

    return {
        "vwap": round(vwap, 4),
        "cost_usd": round(cost_usd, 4),
        "shares":   round(shares, 3),
        "filled_ratio": round(filled_ratio, 4),
        "levels": levels,
        "best_ask": best_ask,
        "best_bid": best_bid,
        "spread":   round(spread, 4),
        "book_depth_usd": round(total_depth_usd, 2),
        "slippage_bps_vs_top": round(slippage_bps, 1),
        "last_fill_price": last_price_hit,
    }


def get_polymarket_resolution(slug, timeout_s=RESOLUTION_TIMEOUT):
    """Poll Gamma for resolution. Two signals, checked in order:
      1. `closed: True`  → formal resolution (Gamma lags real resolution)
      2. `outcomePrices` pinned at extreme (|up - 0.5| > 0.48) — fast path
    Returns (outcome ±1 or 0 for tie, polled_seconds) or (None, None) on timeout."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        m = get_market_by_slug(slug, include_closed=True)
        if m:
            op = m["outcome_prices"]
            if isinstance(op, str):
                op = json.loads(op)
            if op:
                up = float(op[0])
                if abs(up - 0.5) >= 0.48:
                    return (+1 if up > 0.5 else -1), int(time.time() - t0)
                if m["closed"]:
                    if up > 0.5:
                        return +1, int(time.time() - t0)
                    if up < 0.5:
                        return -1, int(time.time() - t0)
                    return 0, int(time.time() - t0)
        time.sleep(3)
    return None, None


def get_binance_outcome(start_ts_sec):
    try:
        r = requests.get(BINANCE, params={
            "symbol": "BTCUSDT", "interval": "5m",
            "startTime": start_ts_sec * 1000, "limit": 2}, timeout=10).json()
        if not r:
            return 0
        op, cp = float(r[0][1]), float(r[0][4])
        return +1 if cp > op else -1 if cp < op else 0
    except Exception:
        return 0


# ---------- signal ----------
def compute_signal(bars):
    if len(bars) < 30:
        return None, 0, "init", {}
    closes = [b["close"] for b in bars]
    rets   = [(b["close"] / b["open"] - 1) for b in bars]
    vols   = [b["volume"] for b in bars]
    brs    = [(b["taker_buy_base"] / b["volume"]) if b["volume"] > 0 else 0.5 for b in bars]

    ret_1  = rets[-1]
    last24 = rets[-24:]
    mean   = sum(last24) / 24
    sigma  = (sum((r - mean) ** 2 for r in last24) / 24) ** 0.5 or 1e-6
    z_prev = ret_1 / sigma
    ret_12 = (closes[-1] / closes[-13]) - 1 if len(closes) >= 14 else 0
    ret_48 = (closes[-1] / closes[-49]) - 1 if len(closes) >= 50 else 0   # 4h trend
    br_1   = brs[-1]
    vol_1  = vols[-1]
    vwin   = vols[-288:] if len(vols) >= 288 else vols
    vma    = sum(vwin) / len(vwin)
    vsd    = (sum((v - vma) ** 2 for v in vwin) / len(vwin)) ** 0.5 or 1
    vol_z  = (vol_1 - vma) / vsd
    hour   = datetime.fromtimestamp(bars[-1]["open_time"] / 1000, tz=timezone.utc).hour

    diag = {"z": round(z_prev, 2), "r12": round(ret_12, 4), "r48": round(ret_48, 4),
            "br": round(br_1, 3), "vz": round(vol_z, 2), "h": hour}

    if hour in LOW_LIQ_HOURS:
        return None, 0, "skip_hour", diag
    if abs(vol_z) > 4:
        return None, 0, "skip_volext", diag

    # Bear-market filter — observed 2026-04-23: mean-reversion UP bets fail
    # during sustained selloffs (capitulation > reversion at 5m scale).
    BEAR_R48 = -0.015
    bear_regime = ret_48 < BEAR_R48

    if z_prev >  3.0:
        return -1, 0.020, "A-z>3-DOWN", diag
    if z_prev < -3.0:
        if bear_regime:
            return None, 0, "skip_bear_regime_A", diag
        return +1, 0.020, "A-z<-3-UP", diag
    if ret_12 > 0.005 and br_1 > 0.55:
        return -1, 0.010, "C-trend+tbd-DOWN", diag
    if ret_12 < -0.005 and br_1 < 0.45:
        if bear_regime:
            return None, 0, "skip_bear_regime_C", diag
        return +1, 0.010, "C-trend+tbd-UP", diag
    return None, 0, "no_signal", diag


def next_5m_boundary(dt=None):
    dt = dt or datetime.now(timezone.utc)
    m = dt.minute - (dt.minute % 5)
    return dt.replace(minute=m, second=0, microsecond=0) + timedelta(minutes=5)
