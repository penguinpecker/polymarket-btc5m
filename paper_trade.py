"""
Paper-trading bot — REAL-WORLD faithful simulator.

Models every execution factor that matters:

  • Chainlink resolution     :  polls Polymarket Gamma for the actual
                                resolved outcomePrices (not Binance open/close).
                                Binance outcome is recorded alongside as
                                a control, so basis flips are visible.
  • Full orderbook walk      :  fetches live CLOB book at T+30s, walks asks
                                to fill the stake. VWAP, not top-of-book.
  • Latency model            :  measures median RTT to Polymarket at boot.
                                Adds latency offset to observation time so
                                the book we see is the book "our order"
                                would actually have hit.
  • Depth-scaled stake cap   :  caps notional at 10% of same-side book depth.
                                Also hard $2000 cap. Prevents 1-trade blowups
                                on thin markets.
  • Partial fills            :  if book depth < intended stake, fill what's
                                available at VWAP. Log partial_fill_ratio.
  • Spread filter            :  skips if (best_ask − best_bid) > 4c.
                                Illiquid markets = adverse selection city.
  • Liquidity filter         :  skips if total same-side depth < $300.
  • Gas cost                 :  subtracts $0.01 flat per trade (Polygon USDC tx).
  • Resolution fallback      :  if Polymarket hasn't resolved within 120s,
                                falls back to Binance and flags the trade.
  • Full telemetry           :  every trade logs vwap, top-of-book bid/ask,
                                spread, book depth, filled_ratio, levels
                                walked, latency sample, Chainlink outcome,
                                Binance outcome, basis flip flag, gas.

State files:
  paper/state.json     bankroll, peak, DD, counters, rolling stats
  paper/trades.csv     one row per closed trade, 18 columns
  paper/tick.log       human-readable event stream
"""
import json, time, csv, pathlib, sys, socket, statistics
from datetime import datetime, timezone, timedelta
import requests

# ---------- ISP DNS bypass (India ISP hijacks polymarket.com) ----------
_DOH_CACHE = {}
_DOH_TTL   = 300

def _doh_resolve(host):
    now = time.time()
    hit = _DOH_CACHE.get(host)
    if hit and hit[1] > now: return hit[0]
    try:
        r = requests.get("https://cloudflare-dns.com/dns-query",
                         params={"name": host, "type":"A"},
                         headers={"accept":"application/dns-json"}, timeout=5)
        ips = [a["data"] for a in r.json().get("Answer", []) if a.get("type") == 1]
        if ips:
            _DOH_CACHE[host] = (ips, now + _DOH_TTL)
            return ips
    except Exception: pass
    return []

_orig_getaddrinfo = socket.getaddrinfo
def _patched_getaddrinfo(host, *a, **kw):
    if host and host.endswith("polymarket.com"):
        for ip in _doh_resolve(host):
            try: return _orig_getaddrinfo(ip, *a, **kw)
            except Exception: continue
    return _orig_getaddrinfo(host, *a, **kw)
socket.getaddrinfo = _patched_getaddrinfo

# ---------- paths & constants ----------
ROOT   = pathlib.Path(__file__).parent / "paper"
ROOT.mkdir(exist_ok=True)
STATE  = ROOT / "state.json"
TRADES = ROOT / "trades.csv"
LOG    = ROOT / "tick.log"

BINANCE = "https://api.binance.com/api/v3/klines"
GAMMA   = "https://gamma-api.polymarket.com"
CLOB    = "https://clob.polymarket.com"

BASE_PRICE_FALLBACK = 0.505     # only used if book fetch fails entirely
MAX_STAKE           = 2000.00
FIXED_STAKE         = 20.00     # flat stake per trade (overrides tier size_frac)
SAFETY_FRAC         = 0.50      # never stake more than this fraction of bankroll
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

# will be set at boot
LATENCY_MS = 250                # round-trip to Polymarket (India baseline)

# ---------- logging / state ----------
def log(msg):
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}  {msg}\n"
    with open(LOG, "a") as f: f.write(line)
    print(line, end="", flush=True)

def load_state():
    if STATE.exists(): return json.loads(STATE.read_text())
    return {
        "bankroll": 100.00, "start_bankroll": 100.00,
        "start_iso": datetime.now(timezone.utc).isoformat(),
        "trades": 0, "wins": 0, "losses": 0,
        "peak": 100.00, "max_dd_pct": 0.0,
        "open_trade": None,
        "polymarket_reachable": None,
        "latency_ms_median": None,
        "basis_flips": 0,          # Chainlink vs Binance outcome mismatches
        "partial_fills": 0,
        "skipped_spread": 0,
        "skipped_thin": 0,
        "avg_fill_ratio": 1.0,
        "avg_slippage_bps_vs_top": 0.0,
    }

def save_state(s): STATE.write_text(json.dumps(s, indent=2))

TRADES_HEADER = [
    "open_time_utc","close_time_utc","direction","tier","stake_intended",
    "stake_filled","vwap","best_ask","best_bid","spread","book_depth_usd",
    "levels_walked","filled_ratio","binance_outcome","chainlink_outcome",
    "basis_flip","pnl","bankroll_after","latency_ms","fill_source",
    "resolve_source",
]
if not TRADES.exists():
    with open(TRADES, "w", newline="") as f:
        csv.writer(f).writerow(TRADES_HEADER)

# ---------- market data helpers ----------
def fetch_recent_klines(n=60, cutoff_ms=None):
    """Return the last n fully-closed 5m bars.
    cutoff_ms: only keep bars with open_time < cutoff_ms. Defaults to
    (now_utc - 5min) so that the currently-forming bar is excluded even if
    Binance hasn't yet emitted it in the klines response (race we hit at
    boundary crossings)."""
    r = requests.get(BINANCE, params={
        "symbol":"BTCUSDT","interval":"5m","limit": n+2
    }, timeout=10)
    r.raise_for_status()
    rows = r.json()
    if cutoff_ms is None:
        cutoff_ms = int(time.time() * 1000) - 5*60*1000 + 1
    rows = [k for k in rows if k[0] < cutoff_ms]
    rows = rows[-n:]
    return [{"open_time": k[0], "open": float(k[1]), "high": float(k[2]),
             "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
             "taker_buy_base": float(k[9])} for k in rows]

def measure_latency(n=5):
    samples = []
    for _ in range(n):
        t0 = time.time()
        try:
            requests.get(f"{GAMMA}/markets", params={"limit":1}, timeout=6)
            samples.append((time.time() - t0) * 1000)
        except Exception: pass
        time.sleep(0.15)
    if not samples: return None
    samples.sort()
    return samples[len(samples)//2]

def get_market_by_slug(slug, include_closed=False):
    """Gamma /markets defaults to active-only; pass include_closed to reach
    resolved markets (required for reading outcomePrices post-settlement)."""
    try:
        params = {"slug": slug}
        if include_closed: params["closed"] = "true"
        r = requests.get(f"{GAMMA}/markets", params=params, timeout=5)
        if r.status_code != 200: return None
        js = r.json()
        if not js and not include_closed:
            # retry including closed — market may have just resolved
            params["closed"] = "true"
            r = requests.get(f"{GAMMA}/markets", params=params, timeout=5)
            js = r.json() if r.status_code == 200 else []
        if not js: return None
        m = js[0]
        tokens = m.get("clobTokenIds")
        if isinstance(tokens, str): tokens = json.loads(tokens)
        return {
            "slug": slug, "condition_id": m.get("conditionId"),
            "token_up": tokens[0] if tokens else None,
            "token_down": tokens[1] if tokens and len(tokens)>1 else None,
            "closed": bool(m.get("closed")),
            "outcome_prices": m.get("outcomePrices"),
        }
    except Exception: return None

def fetch_book(token_id):
    try:
        r = requests.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=5)
        if r.status_code != 200: return None
        b = r.json()
        bids = sorted([(float(x["price"]), float(x["size"])) for x in b.get("bids", [])], reverse=True)
        asks = sorted([(float(x["price"]), float(x["size"])) for x in b.get("asks", [])])
        return {"bids": bids, "asks": asks}
    except Exception: return None

def walk_ask_book(book, stake_usd, depth_cap_fraction=DEPTH_CAP_FRACTION):
    """Fill stake_usd of notional against the ask side, return fill dict or None."""
    if not book or not book["asks"]: return None
    asks = [(p, s) for (p, s) in book["asks"] if 0.01 < p < 0.99]
    if not asks: return None

    total_depth_usd = sum(p*s for p, s in asks)
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
        if remaining <= 0.01: break
        level_cap = price * size
        take = min(level_cap, remaining)
        cost_usd += take
        shares   += take / price
        remaining -= take
        levels   += 1
        last_price_hit = price

    if shares <= 0: return None
    vwap = cost_usd / shares
    filled_ratio = cost_usd / max(stake_usd, 1e-9)
    slippage_bps = (vwap - best_ask) * 10000  # basis points vs top of book

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
      1. `closed: True`  → formal resolution (Gamma lags real resolution by hours)
      2. `outcomePrices` pinned at extreme (|up - 0.5| > 0.48)  → market price
         has snapped to the Chainlink-decided outcome. This is the fast path.
    Returns (outcome ±1 or 0 for tie, polled_seconds) or (None, None) on timeout.
    Called only after window end, so outcomePrices extremes are always
    interpretable as resolution (can't be mid-trade directional spikes)."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        m = get_market_by_slug(slug, include_closed=True)
        if m:
            op = m["outcome_prices"]
            if isinstance(op, str): op = json.loads(op)
            if op:
                up = float(op[0])
                # Fast path: price already at resolution extreme
                if abs(up - 0.5) >= 0.48:
                    return (+1 if up > 0.5 else -1), int(time.time() - t0)
                # Slow path: formal `closed` flip
                if m["closed"]:
                    if up > 0.5:  return +1, int(time.time() - t0)
                    if up < 0.5:  return -1, int(time.time() - t0)
                    return 0, int(time.time() - t0)
        time.sleep(3)
    return None, None

def get_binance_outcome(start_ts_sec):
    try:
        r = requests.get(BINANCE, params={
            "symbol":"BTCUSDT","interval":"5m",
            "startTime": start_ts_sec * 1000, "limit": 2}, timeout=10).json()
        if not r: return 0
        op, cp = float(r[0][1]), float(r[0][4])
        return +1 if cp > op else -1 if cp < op else 0
    except Exception: return 0

# ---------- signal ----------
def compute_signal(bars):
    if len(bars) < 30: return None, 0, "init", {}
    closes = [b["close"] for b in bars]
    rets   = [(b["close"]/b["open"] - 1) for b in bars]
    vols   = [b["volume"] for b in bars]
    brs    = [(b["taker_buy_base"]/b["volume"]) if b["volume"]>0 else 0.5 for b in bars]

    ret_1  = rets[-1]
    last24 = rets[-24:]
    mean   = sum(last24)/24
    sigma  = (sum((r-mean)**2 for r in last24)/24)**0.5 or 1e-6
    z_prev = ret_1 / sigma
    ret_12 = (closes[-1] / closes[-13]) - 1 if len(closes) >= 14 else 0
    ret_48 = (closes[-1] / closes[-49]) - 1 if len(closes) >= 50 else 0   # 4h trend
    br_1   = brs[-1]
    vol_1  = vols[-1]
    vwin   = vols[-288:] if len(vols) >= 288 else vols
    vma    = sum(vwin)/len(vwin)
    vsd    = (sum((v-vma)**2 for v in vwin)/len(vwin))**0.5 or 1
    vol_z  = (vol_1 - vma) / vsd
    hour   = datetime.fromtimestamp(bars[-1]["open_time"]/1000, tz=timezone.utc).hour

    diag = {"z":round(z_prev,2),"r12":round(ret_12,4),"r48":round(ret_48,4),
            "br":round(br_1,3),"vz":round(vol_z,2),"h":hour}

    if hour in LOW_LIQ_HOURS:   return None, 0, "skip_hour",   diag
    if abs(vol_z) > 4:          return None, 0, "skip_volext", diag

    # BEAR-MARKET FILTER (observed 2026-04-23, 20 UP trades 40% WR vs 21 DOWN 57% WR).
    # Mean-reversion UP bets fail during sustained selloffs (bear capitulation
    # > reversion at 5m scale). Skip UP direction if 4h return < -1.5%.
    BEAR_R48 = -0.015
    bear_regime = ret_48 < BEAR_R48

    if z_prev >  3.0: return -1, 0.020, "A-z>3-DOWN",  diag
    if z_prev < -3.0:
        if bear_regime: return None, 0, "skip_bear_regime_A", diag
        return +1, 0.020, "A-z<-3-UP",   diag
    # Tier B DISABLED after 0/5 live (p=2% at historical 54%) — refusing to trade on that.
    # if z_prev >  2.0: return -1, 0.015, "B-z>2-DOWN",  diag
    # if z_prev < -2.0: return +1, 0.015, "B-z<-2-UP",   diag
    if ret_12 >  0.005 and br_1 > 0.55: return -1, 0.010, "C-trend+tbd-DOWN", diag
    if ret_12 < -0.005 and br_1 < 0.45:
        if bear_regime: return None, 0, "skip_bear_regime_C", diag
        return +1, 0.010, "C-trend+tbd-UP",   diag
    return None, 0, "no_signal", diag

def next_5m_boundary(dt=None):
    dt = dt or datetime.now(timezone.utc)
    m = dt.minute - (dt.minute % 5)
    return dt.replace(minute=m, second=0, microsecond=0) + timedelta(minutes=5)

# ---------- execution ----------
def try_enter_at_next_boundary(s):
    """Sleep to next 5m boundary, compute signal, model fill, open trade."""
    now = datetime.now(timezone.utc)
    next_bar = next_5m_boundary(now)
    sl = (next_bar - now).total_seconds() - 1
    if sl > 0: time.sleep(sl)
    while datetime.now(timezone.utc) < next_bar: time.sleep(0.1)

    window_start = int(next_bar.timestamp())
    try:
        # cutoff = start of current window; keep only strictly-earlier bars
        bars = fetch_recent_klines(60, cutoff_ms=window_start * 1000)
    except Exception as e:
        log(f"ERR fetch_klines: {e}"); time.sleep(10); return

    direction, size_frac, tier, diag = compute_signal(bars)
    if direction is None:
        log(f"SKIP  ws={window_start}  {tier}  {diag}")
        save_state(s); return

    # wait T+30s into window, then add simulated latency offset so the book
    # we fetch is the book our order would actually have hit
    obs_target = window_start + T_ENTRY_OFFSET_S + (LATENCY_MS / 1000.0)
    remaining = obs_target - time.time()
    if remaining > 0: time.sleep(remaining)

    slug = f"btc-updown-5m-{window_start}"
    m = get_market_by_slug(slug)
    if not m or not (m["token_up"] and m["token_down"]):
        log(f"SKIP  ws={window_start}  {tier}  no market ({slug})")
        save_state(s); return

    token = m["token_up"] if direction == +1 else m["token_down"]

    # Measure latency on THIS call specifically
    t0 = time.time()
    book = fetch_book(token)
    call_ms = (time.time() - t0) * 1000

    if not book:
        # fallback: assume base price, treat as partial simulation
        if s["polymarket_reachable"] is not False:
            log(f"NOTICE  book fetch failed; using fallback price {BASE_PRICE_FALLBACK}")
        s["polymarket_reachable"] = False
        stake = min(FIXED_STAKE, s["bankroll"] * SAFETY_FRAC, MAX_STAKE)
        s["open_trade"] = {
            "window_start_ts": window_start, "direction": direction, "tier": tier,
            "stake_intended": round(stake,4), "stake_filled": round(stake,4),
            "vwap": BASE_PRICE_FALLBACK, "best_ask": BASE_PRICE_FALLBACK, "best_bid": 0,
            "spread": 0, "book_depth_usd": 0, "levels_walked": 0,
            "filled_ratio": 1.0, "latency_ms": round(call_ms,1),
            "fill_source": "fallback",
        }
        log(f"ENTER(fb) ws={window_start} {tier} dir={direction:+} stake=${stake:.2f} ep={BASE_PRICE_FALLBACK} diag={diag}")
        save_state(s); return

    s["polymarket_reachable"] = True
    best_ask = book["asks"][0][0] if book["asks"] else 1.0
    best_bid = book["bids"][0][0] if book["bids"] else 0.0
    spread   = best_ask - best_bid
    depth    = sum(p*sz for p,sz in book["asks"] if 0.01 < p < 0.99)

    # filters
    spread_limit = MAX_SPREAD_A if tier.startswith("A") else MAX_SPREAD
    if spread > spread_limit:
        s["skipped_spread"] += 1
        log(f"SKIP  ws={window_start}  {tier}  spread_too_wide bid={best_bid:.3f} ask={best_ask:.3f} (limit={spread_limit})")
        save_state(s); return
    if depth < MIN_DEPTH_USD:
        s["skipped_thin"] += 1
        log(f"SKIP  ws={window_start}  {tier}  thin_book depth=${depth:.0f}")
        save_state(s); return

    intended = min(FIXED_STAKE, s["bankroll"] * SAFETY_FRAC, MAX_STAKE)
    fill = walk_ask_book(book, intended)
    if not fill or fill["cost_usd"] < 0.10:
        log(f"SKIP  ws={window_start}  {tier}  no_fill")
        save_state(s); return

    # Entry-price EV filter: skip if fill price implies negative EV at historical WR
    max_entry = MAX_ENTRY_A if tier.startswith("A") else MAX_ENTRY_C
    if fill["vwap"] > max_entry:
        log(f"SKIP  ws={window_start}  {tier}  entry_too_high vwap={fill['vwap']:.3f} (limit={max_entry})")
        save_state(s); return

    if fill["filled_ratio"] < 0.5:
        s["partial_fills"] += 1

    s["open_trade"] = {
        "window_start_ts": window_start,
        "direction": direction, "tier": tier,
        "stake_intended": round(intended, 4),
        "stake_filled":   round(fill["cost_usd"], 4),
        "vwap":           fill["vwap"],
        "best_ask":       fill["best_ask"],
        "best_bid":       fill["best_bid"],
        "spread":         fill["spread"],
        "book_depth_usd": fill["book_depth_usd"],
        "levels_walked":  fill["levels"],
        "filled_ratio":   fill["filled_ratio"],
        "latency_ms":     round(call_ms, 1),
        "fill_source":    "live",
        "slug":           slug,
        "diag": diag,
    }
    log(f"ENTER  ws={window_start}  {tier}  dir={direction:+}  "
        f"filled=${fill['cost_usd']:.2f}/{intended:.2f} "
        f"vwap={fill['vwap']:.4f} top={fill['best_ask']:.3f} "
        f"spr={fill['spread']:.3f} depth=${fill['book_depth_usd']:.0f} "
        f"lvl={fill['levels']} slip={fill['slippage_bps_vs_top']}bps "
        f"lat={call_ms:.0f}ms  {diag}")
    save_state(s)

def resolve_open_trade(s):
    t = s["open_trade"]
    close_ts = t["window_start_ts"] + 300
    sleep_s = max(0, close_ts + 5 - time.time())
    if sleep_s > 0:
        log(f"WAIT  open {t['tier']} resolves in {sleep_s:.0f}s")
        time.sleep(sleep_s)

    binance_outcome = get_binance_outcome(t["window_start_ts"])
    slug = t.get("slug", f"btc-updown-5m-{t['window_start_ts']}")
    chain_outcome, poll_s = get_polymarket_resolution(slug, timeout_s=RESOLUTION_TIMEOUT)
    if chain_outcome is None:
        chain_outcome = binance_outcome
        resolve_source = "binance_fallback"
    else:
        resolve_source = f"polymarket({poll_s}s)"

    basis_flip = (binance_outcome != chain_outcome and chain_outcome != 0 and binance_outcome != 0)
    if basis_flip: s["basis_flips"] += 1

    outcome = chain_outcome
    stake   = t["stake_filled"]
    ep      = t["vwap"]

    win = (t["direction"] == outcome)
    if outcome == 0:
        pnl = 0.0  # tie → refund
    elif win:
        pnl = stake * (1.0/ep - 1.0) - GAS_COST_USD
        s["wins"] += 1
    else:
        pnl = -stake - GAS_COST_USD
        s["losses"] += 1
    if outcome != 0: s["trades"] += 1
    s["bankroll"] += pnl
    s["peak"]      = max(s["peak"], s["bankroll"])
    dd = (s["peak"] - s["bankroll"]) / s["peak"] * 100
    s["max_dd_pct"] = max(s["max_dd_pct"], dd)

    # rolling averages
    n = max(s["trades"], 1)
    prev_ratio = s.get("avg_fill_ratio", 1.0)
    s["avg_fill_ratio"] = ((prev_ratio*(n-1)) + t["filled_ratio"]) / n
    prev_slip = s.get("avg_slippage_bps_vs_top", 0.0)
    slip_now  = (t["vwap"] - t["best_ask"]) * 10000 if t["best_ask"] else 0
    s["avg_slippage_bps_vs_top"] = ((prev_slip*(n-1)) + slip_now) / n

    with open(TRADES, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.fromtimestamp(t["window_start_ts"], tz=timezone.utc).isoformat(),
            datetime.fromtimestamp(close_ts, tz=timezone.utc).isoformat(),
            t["direction"], t["tier"],
            f"{t['stake_intended']:.4f}", f"{t['stake_filled']:.4f}",
            f"{t['vwap']:.4f}", f"{t['best_ask']:.4f}", f"{t['best_bid']:.4f}",
            f"{t['spread']:.4f}", f"{t['book_depth_usd']:.2f}",
            t["levels_walked"], f"{t['filled_ratio']:.4f}",
            binance_outcome, chain_outcome,
            int(basis_flip), f"{pnl:+.4f}", f"{s['bankroll']:.4f}",
            f"{t['latency_ms']}", t["fill_source"], resolve_source,
        ])
    log(f"RESOLVE  {t['tier']}  pred={t['direction']:+}  "
        f"bin={binance_outcome:+} chain={chain_outcome:+}{'(FLIP!)' if basis_flip else ''}  "
        f"{'WIN' if win else ('TIE' if outcome==0 else 'LOSS')}  "
        f"pnl={pnl:+.3f}  bankroll=${s['bankroll']:.2f}  "
        f"WR={s['wins']}/{s['trades']}={s['wins']/max(s['trades'],1):.1%}  "
        f"DD={dd:.1f}%  src={resolve_source}")
    s["open_trade"] = None
    save_state(s)

# ---------- main ----------
def main():
    s = load_state()
    lat = measure_latency()
    if lat is not None:
        s["latency_ms_median"] = round(lat, 1)
        global LATENCY_MS
        LATENCY_MS = lat
    log(f"BOOT  bankroll=${s['bankroll']:.2f}  trades={s['trades']}  "
        f"wins={s['wins']}  reach={s['polymarket_reachable']}  "
        f"lat_ms={s.get('latency_ms_median')}")
    save_state(s)

    while True:
        try:
            if s["open_trade"]: resolve_open_trade(s)
            else:               try_enter_at_next_boundary(s)
        except Exception as e:
            import traceback
            log(f"LOOP_ERR  {e}")
            log(traceback.format_exc())
            time.sleep(15)

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: log("BYE")
    except Exception as e:
        import traceback
        log(f"FATAL  {e}\n{traceback.format_exc()}")
        sys.exit(1)
