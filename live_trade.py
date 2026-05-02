"""
Live trading bot — same signal as paper, real Polymarket CLOB orders +
on-chain redemption. Runs as a sibling Railway service to the paper bot.

Loop structure mirrors paper exactly: enter at next 5m boundary -> wait
for window close -> resolve outcome -> if win, redeem on-chain -> record
trade -> repeat. One open position at a time (same as paper) so the
trade cadence is identical for clean side-by-side PnL comparison.

Safety / kill switches
----------------------
LIVE_ENABLED=false (default)   — shadow mode: logs intended order, never posts.
LIVE_DAILY_LOSS_KILL=$30       — stop entries if today's realized PnL ≤ -$30.
LIVE_TOTAL_DD_KILL=$50         — stop entries if bankroll < peak - $50.
LIVE_MIN_BALANCE=$5            — stop entries if USDC.e wallet balance < $5.

Divergence with paper: when live can't fill (no liquidity, balance fail,
API error) we LOG and SKIP, never retry the same window — so PnL deltas
isolate to fill quality, not retry timing.

Secrets — Railway env only, never logged, never written to volume:
  LIVE_PRIVATE_KEY, LIVE_CLOB_API_KEY, LIVE_CLOB_SECRET,
  LIVE_CLOB_PASSPHRASE, POLYGON_RPC.
"""
import os
import sys
import json
import time
import csv
import pathlib
import traceback
from datetime import datetime, timezone

# Shared signal logic with paper — DO NOT duplicate it. The byte-identical
# signal is the whole point of the comparison.
from paper_trade import (
    compute_signal,
    fetch_recent_klines,
    fetch_book,
    walk_ask_book,
    get_market_by_slug,
    get_polymarket_resolution,
    get_binance_outcome,
    next_5m_boundary,
    MIN_DEPTH_USD,
    MAX_SPREAD,
    MAX_SPREAD_A,
    MAX_ENTRY_C,
    MAX_ENTRY_A,
    GAS_COST_USD,
    T_ENTRY_OFFSET_S,
    RESOLUTION_TIMEOUT,
)

# ---------- paths ----------
ROOT = pathlib.Path(os.environ.get("LIVE_DATA_DIR", "/app/live"))
ROOT.mkdir(parents=True, exist_ok=True)
STATE     = ROOT / "state.json"
TRADES    = ROOT / "trades.csv"
POSITIONS = ROOT / "positions.json"
LOG       = ROOT / "tick.log"

# ---------- env / config ----------
LIVE_ENABLED       = os.environ.get("LIVE_ENABLED", "false").lower() == "true"
LIVE_STAKE_USD     = float(os.environ.get("LIVE_STAKE_USD", "5.0"))
LIVE_DAILY_LOSS    = float(os.environ.get("LIVE_DAILY_LOSS_KILL", "30.0"))
LIVE_TOTAL_DD_KILL = float(os.environ.get("LIVE_TOTAL_DD_KILL", "50.0"))
LIVE_MIN_BALANCE   = float(os.environ.get("LIVE_MIN_BALANCE", "5.0"))
LIVE_REDEEM_GAS_GW = float(os.environ.get("LIVE_REDEEM_GAS_GWEI_CAP", "300"))
LIVE_MAX_REDEEM_TRIES = int(os.environ.get("LIVE_MAX_REDEEM_TRIES", "5"))
POLYGON_RPC        = os.environ.get("POLYGON_RPC", "https://polygon-rpc.com")

# v2 architecture (CLOB v2, post 2026-04-30):
#  - LIVE_FUNDER:         the Polymarket-managed Safe address. Holds pUSD
#                         + outcome tokens. Order maker. Defaults to EOA
#                         (legacy v1 mode — DOES NOT WORK on v2 markets).
#  - LIVE_SIGNATURE_TYPE: "EOA" | "POLY_PROXY" | "POLY_GNOSIS_SAFE" | "POLY_1271".
#                         For wallets onboarded via polymarket.com on v2,
#                         the Safe is sig_type=POLY_GNOSIS_SAFE.
PRIVATE_KEY = os.environ.get("LIVE_PRIVATE_KEY", "")
API_KEY     = os.environ.get("LIVE_CLOB_API_KEY", "")
API_SECRET  = os.environ.get("LIVE_CLOB_SECRET", "")
API_PASS    = os.environ.get("LIVE_CLOB_PASSPHRASE", "")
LIVE_FUNDER = os.environ.get("LIVE_FUNDER", "")
LIVE_SIG_TYPE = os.environ.get("LIVE_SIGNATURE_TYPE", "EOA")

DB_URL = os.environ.get("DATABASE_URL", "")
try:
    import psycopg2
    import psycopg2.extras
    HAS_PG = bool(DB_URL)
except ImportError:
    HAS_PG = False


# ---------- logging ----------
def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}  {msg}\n"
    with open(LOG, "a") as f:
        f.write(line)
    print(line, end="", flush=True)


def fp(s: str) -> str:
    if not s:
        return "<unset>"
    return f"...{s[-4:]} (len={len(s)})"


# ---------- state ----------
def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {
        "bankroll":        100.00,
        "start_bankroll":  100.00,
        "start_iso":       datetime.now(timezone.utc).isoformat(),
        "trades":          0,
        "wins":            0,
        "losses":          0,
        "peak":            100.00,
        "max_dd_pct":      0.0,
        "daily_pnl":       {},
        "killed_by":       None,
        "shadow_skips":    0,
        "fill_failures":   0,
    }


def save_state(s: dict) -> None:
    STATE.write_text(json.dumps(s, indent=2))


def load_positions() -> list:
    if POSITIONS.exists():
        return json.loads(POSITIONS.read_text())
    return []


def save_positions(ps: list) -> None:
    POSITIONS.write_text(json.dumps(ps, indent=2))


TRADES_HEADER = [
    "open_time_utc", "close_time_utc", "direction", "tier",
    "stake_intended", "stake_filled", "vwap", "best_ask", "best_bid",
    "spread", "book_depth_usd", "filled_ratio", "binance_outcome",
    "chain_outcome", "basis_flip", "expected_pnl", "realized_pnl",
    "bankroll_after", "latency_ms", "fill_source", "resolve_source",
    "order_id", "redeem_tx",
]
if not TRADES.exists():
    with open(TRADES, "w", newline="") as f:
        csv.writer(f).writerow(TRADES_HEADER)


# ---------- Postgres mirror ----------
_DDL = """
CREATE TABLE IF NOT EXISTS live_trades (
  open_time_utc     TIMESTAMPTZ PRIMARY KEY,
  close_time_utc    TIMESTAMPTZ,
  direction         SMALLINT,
  tier              TEXT,
  stake_intended    NUMERIC,
  stake_filled      NUMERIC,
  vwap              NUMERIC,
  best_ask          NUMERIC,
  best_bid          NUMERIC,
  spread            NUMERIC,
  book_depth_usd    NUMERIC,
  filled_ratio      NUMERIC,
  binance_outcome   SMALLINT,
  chain_outcome     SMALLINT,
  basis_flip        SMALLINT,
  expected_pnl      NUMERIC,
  realized_pnl      NUMERIC,
  bankroll_after    NUMERIC,
  latency_ms        NUMERIC,
  fill_source       TEXT,
  resolve_source    TEXT,
  order_id          TEXT,
  redeem_tx         TEXT,
  recorded_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS live_state (
  id          INT PRIMARY KEY DEFAULT 1,
  state       JSONB NOT NULL,
  updated_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS live_positions (
  position_id   TEXT PRIMARY KEY,
  state         JSONB NOT NULL,
  status        TEXT,
  updated_at    TIMESTAMPTZ DEFAULT NOW()
);
"""

_INSERT_TRADE = """
INSERT INTO live_trades (open_time_utc, close_time_utc, direction, tier,
    stake_intended, stake_filled, vwap, best_ask, best_bid, spread,
    book_depth_usd, filled_ratio, binance_outcome, chain_outcome,
    basis_flip, expected_pnl, realized_pnl, bankroll_after, latency_ms,
    fill_source, resolve_source, order_id, redeem_tx)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (open_time_utc) DO UPDATE SET
    realized_pnl=EXCLUDED.realized_pnl,
    bankroll_after=EXCLUDED.bankroll_after,
    redeem_tx=EXCLUDED.redeem_tx,
    chain_outcome=EXCLUDED.chain_outcome,
    resolve_source=EXCLUDED.resolve_source
"""


def _db_conn():
    if not HAS_PG:
        return None
    try:
        return psycopg2.connect(DB_URL, connect_timeout=5)
    except Exception as e:
        print(f"[db] connect failed: {e}", flush=True)
        return None


def db_init() -> None:
    conn = _db_conn()
    if not conn:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute(_DDL)
    except Exception as e:
        print(f"[db] init failed: {e}", flush=True)
    finally:
        conn.close()


def db_upsert_trade(row) -> None:
    conn = _db_conn()
    if not conn:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute(_INSERT_TRADE, row)
    except Exception as e:
        print(f"[db] upsert_trade failed: {e}", flush=True)
    finally:
        conn.close()


def db_save_state(s: dict) -> None:
    conn = _db_conn()
    if not conn:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO live_state (id, state, updated_at) VALUES (1, %s, NOW())
                ON CONFLICT (id) DO UPDATE SET state=EXCLUDED.state, updated_at=NOW()
            """, (json.dumps(s),))
    except Exception as e:
        print(f"[db] save_state failed: {e}", flush=True)
    finally:
        conn.close()


def db_save_position(pos: dict) -> None:
    conn = _db_conn()
    if not conn:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO live_positions (position_id, state, status, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (position_id) DO UPDATE SET
                    state=EXCLUDED.state, status=EXCLUDED.status, updated_at=NOW()
            """, (pos["id"], json.dumps(pos), pos.get("status", "open")))
    except Exception as e:
        print(f"[db] save_position failed: {e}", flush=True)
    finally:
        conn.close()


# ---------- chain + clob clients (lazy) ----------
_w3 = None
_account = None
_clob = None


def get_chain():
    global _w3, _account
    if _w3 is None or _account is None:
        from pm_chain import make_w3, load_account
        _w3 = make_w3(POLYGON_RPC)
        _account = load_account(PRIVATE_KEY)
    return _w3, _account


def get_clob():
    global _clob
    if _clob is None:
        from pm_clob import make_client
        _clob = make_client(
            PRIVATE_KEY,
            API_KEY or None, API_SECRET or None, API_PASS or None,
            signature_type_name=LIVE_SIG_TYPE,
            funder=LIVE_FUNDER or None,
        )
    return _clob


# ---------- kill switches ----------
def check_kill_switches(s: dict) -> str | None:
    if s.get("killed_by"):
        return s["killed_by"]
    today = datetime.now(timezone.utc).date().isoformat()
    today_pnl = s.get("daily_pnl", {}).get(today, 0.0)
    if today_pnl <= -LIVE_DAILY_LOSS:
        s["killed_by"] = f"daily_loss({today_pnl:.2f}<=-{LIVE_DAILY_LOSS})"
        return s["killed_by"]
    dd = s["peak"] - s["bankroll"]
    if dd >= LIVE_TOTAL_DD_KILL:
        s["killed_by"] = f"total_dd({dd:.2f}>={LIVE_TOTAL_DD_KILL})"
        return s["killed_by"]
    return None


def check_balance() -> tuple[bool, float]:
    """Read trading collateral balance. v2: pUSD on the Safe (LIVE_FUNDER).
    v1 / legacy / no-funder: USDC.e on the EOA. Returns (ok, balance_usd)."""
    try:
        w3, acct = get_chain()
        if LIVE_FUNDER:
            from pm_chain import pusd_balance
            bal = pusd_balance(w3, LIVE_FUNDER)
        else:
            from pm_chain import usdc_balance
            bal = usdc_balance(w3, acct.address)
    except Exception as e:
        log(f"BALANCE_ERR  {type(e).__name__}: {e}")
        return False, 0.0
    return bal >= LIVE_MIN_BALANCE, bal


# ---------- entry ----------
def try_enter_at_next_boundary(s: dict, positions: list) -> None:
    now = datetime.now(timezone.utc)
    next_bar = next_5m_boundary(now)
    sl = (next_bar - now).total_seconds() - 1
    if sl > 0:
        time.sleep(sl)
    while datetime.now(timezone.utc) < next_bar:
        time.sleep(0.1)

    window_start = int(next_bar.timestamp())
    try:
        bars = fetch_recent_klines(60, cutoff_ms=window_start * 1000)
    except Exception as e:
        log(f"ERR fetch_klines: {e}")
        time.sleep(10)
        return

    direction, _size_frac, tier, diag = compute_signal(bars)
    if direction is None:
        log(f"SKIP  ws={window_start}  {tier}  {diag}")
        return

    obs_target = window_start + T_ENTRY_OFFSET_S
    remaining = obs_target - time.time()
    if remaining > 0:
        time.sleep(remaining)

    slug = f"btc-updown-5m-{window_start}"
    m = get_market_by_slug(slug)
    if not m or not (m["token_up"] and m["token_down"]):
        log(f"SKIP  ws={window_start}  {tier}  no market ({slug})")
        return

    token_id = m["token_up"] if direction == +1 else m["token_down"]
    book = fetch_book(token_id)
    if not book:
        log(f"SKIP  ws={window_start}  {tier}  no book")
        s["fill_failures"] += 1
        save_state(s)
        return

    best_ask = book["asks"][0][0] if book["asks"] else 1.0
    best_bid = book["bids"][0][0] if book["bids"] else 0.0
    spread = best_ask - best_bid
    depth = sum(p * sz for p, sz in book["asks"] if 0.01 < p < 0.99)

    spread_limit = MAX_SPREAD_A if tier.startswith("A") else MAX_SPREAD
    if spread > spread_limit:
        log(f"SKIP  ws={window_start}  {tier}  spread_too_wide bid={best_bid:.3f} ask={best_ask:.3f}")
        return
    if depth < MIN_DEPTH_USD:
        log(f"SKIP  ws={window_start}  {tier}  thin_book depth=${depth:.0f}")
        return

    fill_est = walk_ask_book(book, LIVE_STAKE_USD)
    if not fill_est or fill_est["cost_usd"] < 0.10:
        log(f"SKIP  ws={window_start}  {tier}  no_fill_est")
        return

    max_entry = MAX_ENTRY_A if tier.startswith("A") else MAX_ENTRY_C
    if fill_est["vwap"] > max_entry:
        log(f"SKIP  ws={window_start}  {tier}  entry_too_high vwap={fill_est['vwap']:.3f}")
        return

    killed = check_kill_switches(s)
    if killed:
        log(f"SKIP  ws={window_start}  {tier}  killed_by={killed}")
        save_state(s)
        return
    bal_ok, bal = check_balance()
    if not bal_ok:
        log(f"SKIP  ws={window_start}  {tier}  low_balance ${bal:.2f}<{LIVE_MIN_BALANCE}")
        return

    if not LIVE_ENABLED:
        s["shadow_skips"] += 1
        log(f"SHADOW  ws={window_start}  {tier}  dir={direction:+}  "
            f"would_buy token={token_id[:10]}.. notional=${LIVE_STAKE_USD:.2f} "
            f"top={best_ask:.3f} spr={spread:.3f} depth=${depth:.0f} bal=${bal:.2f}  {diag}")
        save_state(s)
        return

    t0 = time.time()
    try:
        from pm_clob import place_market_buy_fok
        client = get_clob()
        resp = place_market_buy_fok(client, token_id, LIVE_STAKE_USD)
    except Exception as e:
        log(f"ORDER_ERR  ws={window_start}  {tier}  {type(e).__name__}: {e}")
        s["fill_failures"] += 1
        save_state(s)
        return
    call_ms = (time.time() - t0) * 1000

    if not resp or not resp.get("success"):
        log(f"ORDER_FAIL  ws={window_start}  {tier}  resp={resp}")
        s["fill_failures"] += 1
        save_state(s)
        return

    order_id = resp.get("orderID") or resp.get("order_id") or ""
    making = float(resp.get("makingAmount", 0) or 0)
    taking = float(resp.get("takingAmount", 0) or 0)
    if taking <= 0 or making <= 0:
        log(f"ORDER_NOFILL  ws={window_start}  {tier}  resp={resp}")
        s["fill_failures"] += 1
        save_state(s)
        return

    actual_vwap = making / taking
    pos = {
        "id":               f"{window_start}-{direction}",
        "window_start_ts":  window_start,
        "close_ts":         window_start + 300,
        "direction":        direction,
        "tier":             tier,
        "slug":             slug,
        "condition_id":     m.get("condition_id"),
        "token_id":         token_id,
        "stake_intended":   round(LIVE_STAKE_USD, 4),
        "stake_filled":     round(making, 4),
        "shares":           round(taking, 6),
        "vwap":             round(actual_vwap, 4),
        "best_ask":         round(best_ask, 4),
        "best_bid":         round(best_bid, 4),
        "spread":           round(spread, 4),
        "book_depth_usd":   round(depth, 2),
        "filled_ratio":     round(making / LIVE_STAKE_USD, 4),
        "latency_ms":       round(call_ms, 1),
        "order_id":         order_id,
        "status":           "filled",
        "redeem_attempts":  0,
        "diag":             diag,
    }
    positions.append(pos)
    save_positions(positions)
    db_save_position(pos)
    log(f"LIVE_FILL  ws={window_start}  {tier}  dir={direction:+}  "
        f"got={taking:.3f}sh @ {actual_vwap:.4f}  cost=${making:.2f}/{LIVE_STAKE_USD:.2f}  "
        f"top={best_ask:.3f} lat={call_ms:.0f}ms  oid={order_id[:10]}..")
    save_state(s)


# ---------- resolution + redemption (synchronous) ----------
def handle_open_position(pos: dict, s: dict, positions: list) -> None:
    """Block until pos reaches a final state (done | redeemed | redeem_abandoned)."""

    # 1) Wait for window close, then determine outcome.
    #    Priority: on-chain CTF.payoutNumerators (most reliable on v2;
    #    Polymarket Gamma's `closed=true` filter is unreliable post-cutover).
    #    Fall back to Gamma `outcomePrices` extreme, then Binance.
    if pos["status"] == "filled":
        wait_s = pos["close_ts"] + 5 - time.time()
        if wait_s > 0:
            log(f"WAIT  open {pos['tier']} {pos['id']} resolves in {wait_s:.0f}s")
            time.sleep(wait_s)

        binance_outcome = get_binance_outcome(pos["window_start_ts"])
        chain_outcome = None
        resolve_source = None

        # Primary: on-chain via CTF.payoutNumerators
        cid = pos.get("condition_id")
        if cid:
            try:
                from pm_chain import is_resolved, payout_numerators
                w3, _ = get_chain()
                t0 = time.time()
                while time.time() - t0 < RESOLUTION_TIMEOUT:
                    if is_resolved(w3, cid):
                        n_up, n_down = payout_numerators(w3, cid)
                        if n_up > 0 and n_down == 0:
                            chain_outcome = +1
                        elif n_down > 0 and n_up == 0:
                            chain_outcome = -1
                        else:
                            chain_outcome = 0
                        resolve_source = f"chain({int(time.time()-t0)}s)"
                        break
                    time.sleep(15)
            except Exception as e:
                log(f"CHAIN_RESOLVE_ERR  {pos['id']}  {type(e).__name__}: {e}")

        # Secondary: Gamma (often lags but cheap to try)
        if chain_outcome is None:
            chain_outcome, poll_s = get_polymarket_resolution(pos["slug"], timeout_s=60)
            if chain_outcome is not None:
                resolve_source = f"polymarket({poll_s}s)"

        # Last resort: Binance
        if chain_outcome is None:
            chain_outcome = binance_outcome
            resolve_source = "binance_fallback"

        pos["binance_outcome"] = binance_outcome
        pos["chain_outcome"]   = chain_outcome
        pos["resolve_source"]  = resolve_source
        pos["status"]          = "resolved"
        save_positions(positions)
        db_save_position(pos)

    won = (pos["direction"] == pos["chain_outcome"])
    is_tie = (pos["chain_outcome"] == 0)
    stake = pos["stake_filled"]
    vwap = pos["vwap"]
    if is_tie:
        expected_pnl = 0.0
    elif won:
        expected_pnl = stake * (1.0 / vwap - 1.0) - GAS_COST_USD
    else:
        expected_pnl = -stake - GAS_COST_USD
    pos["expected_pnl"] = round(expected_pnl, 4)

    # 2) Redeem if winner. Loser/tie tokens are worthless — paper accounting
    #    and live accounting agree: PnL = -stake (or 0 on tie). No tx needed.
    redeem_tx = ""
    realized_pnl = expected_pnl
    redeem_status = None

    if won and not is_tie and LIVE_ENABLED:
        # On v2 the funder (Safe) holds the outcome tokens, so redemption
        # must be invoked AS the Safe via execTransaction. Use the Safe
        # path when LIVE_FUNDER is set; otherwise (legacy / v1 / EOA-only)
        # call CTF.redeemPositions directly from the EOA.
        from pm_chain import (
            is_resolved, redeem_positions, safe_redeem_positions,
            wait_receipt, pusd_balance, usdc_balance,
        )
        try:
            w3, acct = get_chain()
        except Exception as e:
            log(f"CHAIN_ERR  {pos['id']}  {type(e).__name__}: {e}")
            pos["status"] = "resolved"  # leave for next iteration retry
            save_positions(positions)
            return

        cid = pos["condition_id"]
        if not cid:
            log(f"REDEEM_SKIP  no condition_id for {pos['id']}")
            redeem_status = "abandoned_no_cid"
        else:
            # Wait for on-chain resolution to land (CTF.payoutDenominator>0)
            chain_wait_start = time.time()
            while not is_resolved(w3, cid):
                if time.time() - chain_wait_start > 600:
                    log(f"REDEEM_TIMEOUT  {pos['id']}  chain not resolved after 600s")
                    redeem_status = "abandoned_chain_timeout"
                    break
                time.sleep(15)
            if redeem_status is None:
                index_set = 1 if pos["direction"] == +1 else 2
                attempts_left = LIVE_MAX_REDEEM_TRIES - pos.get("redeem_attempts", 0)
                while attempts_left > 0:
                    pos["redeem_attempts"] = pos.get("redeem_attempts", 0) + 1
                    try:
                        # Read pUSD-on-Safe (v2) or USDC.e-on-EOA (v1) before redeem
                        if LIVE_FUNDER:
                            bal_before = pusd_balance(w3, LIVE_FUNDER)
                            tx_hash = safe_redeem_positions(
                                w3, acct, LIVE_FUNDER, cid, [index_set],
                                gas_price_gwei_cap=LIVE_REDEEM_GAS_GW,
                            )
                        else:
                            bal_before = usdc_balance(w3, acct.address)
                            tx_hash = redeem_positions(
                                w3, acct, cid, [index_set],
                                gas_price_gwei_cap=LIVE_REDEEM_GAS_GW,
                            )
                        log(f"REDEEM_SENT  {pos['id']}  tx={tx_hash}  attempt={pos['redeem_attempts']}")
                        rcpt = wait_receipt(w3, tx_hash, timeout_s=180)
                        if rcpt.get("status") != 1:
                            log(f"REDEEM_FAIL  {pos['id']}  tx={tx_hash}  rcpt_status={rcpt.get('status')}")
                            attempts_left -= 1
                            time.sleep(10)
                            continue
                        if LIVE_FUNDER:
                            bal_after = pusd_balance(w3, LIVE_FUNDER)
                        else:
                            bal_after = usdc_balance(w3, acct.address)
                        received = bal_after - bal_before
                        realized_pnl = received - stake - GAS_COST_USD
                        redeem_tx = tx_hash
                        log(f"REDEEM_OK    {pos['id']}  recv=${received:.4f}  realized={realized_pnl:+.4f}")
                        break
                    except Exception as e:
                        log(f"REDEEM_ERR   {pos['id']}  attempt={pos['redeem_attempts']}  {type(e).__name__}: {e}")
                        attempts_left -= 1
                        save_positions(positions)
                        db_save_position(pos)
                        time.sleep(15)
                else:
                    redeem_status = "abandoned_max_retries"

        if redeem_status:
            log(f"REDEEM_ABANDONED  {pos['id']}  reason={redeem_status}  "
                f"manual claim required for {pos.get('shares',0)} shares of "
                f"token {pos['token_id'][:12]}..")

    # 3) Finalize trade row + bankroll. Even if redemption was abandoned, we
    #    record the trade with realized=0 (the USDC is locked in tokens
    #    pending manual claim) and tag fill_source for ops awareness.
    fill_source = "live"
    if redeem_status:
        realized_pnl = 0.0   # pending manual claim, no PnL realized yet
        fill_source = f"live_unredeemed:{redeem_status}"

    s["bankroll"] += realized_pnl
    s["peak"] = max(s["peak"], s["bankroll"])
    if realized_pnl > 0:
        s["wins"] += 1
        s["trades"] += 1
    elif realized_pnl < 0:
        s["losses"] += 1
        s["trades"] += 1
    today = datetime.now(timezone.utc).date().isoformat()
    s.setdefault("daily_pnl", {})
    s["daily_pnl"][today] = round(s["daily_pnl"].get(today, 0.0) + realized_pnl, 4)
    dd = (s["peak"] - s["bankroll"]) / s["peak"] * 100 if s["peak"] > 0 else 0
    s["max_dd_pct"] = max(s["max_dd_pct"], dd)

    basis_flip = (pos.get("binance_outcome") != pos["chain_outcome"]
                  and pos["chain_outcome"] != 0
                  and pos.get("binance_outcome") != 0)

    row = [
        datetime.fromtimestamp(pos["window_start_ts"], tz=timezone.utc).isoformat(),
        datetime.fromtimestamp(pos["close_ts"], tz=timezone.utc).isoformat(),
        pos["direction"], pos["tier"],
        f"{pos['stake_intended']:.4f}", f"{pos['stake_filled']:.4f}",
        f"{pos['vwap']:.4f}", f"{pos['best_ask']:.4f}", f"{pos['best_bid']:.4f}",
        f"{pos['spread']:.4f}", f"{pos['book_depth_usd']:.2f}",
        f"{pos['filled_ratio']:.4f}",
        pos.get("binance_outcome", 0), pos["chain_outcome"], int(basis_flip),
        f"{pos['expected_pnl']:+.4f}", f"{realized_pnl:+.4f}",
        f"{s['bankroll']:.4f}", f"{pos['latency_ms']}",
        fill_source, pos.get("resolve_source", "?"),
        pos.get("order_id", ""), redeem_tx,
    ]
    with open(TRADES, "a", newline="") as f:
        csv.writer(f).writerow(row)
    db_upsert_trade(row)

    pos["status"]       = redeem_status or ("redeemed" if (won and not is_tie and LIVE_ENABLED) else "done")
    pos["realized_pnl"] = round(realized_pnl, 4)
    pos["redeem_tx"]    = redeem_tx
    save_positions(positions)
    db_save_position(pos)
    save_state(s)
    db_save_state(s)

    log(f"FINAL  {pos['id']}  {pos['tier']}  pred={pos['direction']:+} chain={pos['chain_outcome']:+}{'(FLIP)' if basis_flip else ''}  "
        f"{'WIN' if won and not is_tie else ('TIE' if is_tie else 'LOSS')}  "
        f"realized=${realized_pnl:+.3f}  bankroll=${s['bankroll']:.2f}  "
        f"WR={s['wins']}/{s['trades']}={s['wins']/max(s['trades'],1):.1%}  "
        f"DD={dd:.1f}%  src={pos.get('resolve_source','?')}")


# ---------- main ----------
def main() -> None:
    s = load_state()
    positions = load_positions()
    db_init()

    log(f"BOOT  mode={'LIVE' if LIVE_ENABLED else 'SHADOW'}  "
        f"stake=${LIVE_STAKE_USD:.2f}  daily_kill=${LIVE_DAILY_LOSS:.0f}  "
        f"dd_kill=${LIVE_TOTAL_DD_KILL:.0f}  min_bal=${LIVE_MIN_BALANCE:.2f}  "
        f"db={'on' if HAS_PG else 'off'}  "
        f"pk={fp(PRIVATE_KEY)}  api_key={fp(API_KEY)}  "
        f"sig={LIVE_SIG_TYPE}  funder={LIVE_FUNDER[:10]+'..' if LIVE_FUNDER else '<EOA>'}")

    if LIVE_ENABLED:
        try:
            from pm_chain import usdc_balance, pusd_balance
            w3, acct = get_chain()
            log(f"EOA     addr={acct.address}  usdc=${usdc_balance(w3, acct.address):.4f}")
            if LIVE_FUNDER:
                log(f"FUNDER  addr={LIVE_FUNDER}  pUSD=${pusd_balance(w3, LIVE_FUNDER):.4f}")
            get_clob()
            log("CLOB    ready")
        except Exception as e:
            log(f"BOOT_FAIL  {type(e).__name__}: {e}")
            log(traceback.format_exc())
            sys.exit(2)
    else:
        log("SHADOW  no secret connectivity check; entries will be logged only.")

    save_state(s)
    db_save_state(s)

    while True:
        try:
            # 1) finalize any stale unfinished positions (from prior boots)
            for pos in list(positions):
                if pos["status"] in ("filled", "resolved"):
                    handle_open_position(pos, s, positions)

            # 2) at next 5m boundary, try to enter
            try_enter_at_next_boundary(s, positions)

            # 3) if we just entered, immediately handle that position
            for pos in list(positions):
                if pos["status"] in ("filled", "resolved"):
                    handle_open_position(pos, s, positions)
        except Exception as e:
            log(f"LOOP_ERR  {type(e).__name__}: {e}")
            log(traceback.format_exc())
            time.sleep(15)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("BYE")
    except Exception as e:
        log(f"FATAL  {type(e).__name__}: {e}\n{traceback.format_exc()}")
        sys.exit(1)
