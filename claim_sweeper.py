"""
Autonomous claim sweeper for the Polymarket Safe.

Runs in an infinite loop. Each iteration:
  1. Asks Polymarket's data API for the Safe's redeemable positions.
  2. For each, calls CTF.redeemPositions via Safe execTransaction
     (winning positions credit USDC.e; losing positions burn worthless
     tokens — both are "claims" that clear the position from the Safe
     so the Polymarket UI stops showing them as a pile).
  3. Wraps any resulting USDC.e on the Safe back to pUSD via the
     Polymarket CollateralOnramp so the bot can spend it on the next
     trade.
  4. Sleeps SWEEP_INTERVAL_S (default 300s = 5 min) and repeats.

Failures on individual positions are logged and retried on the next
iteration — the trading bot keeps running independently.

Env required (same set as live_trade.py):
  LIVE_PRIVATE_KEY, LIVE_FUNDER, POLYGON_RPC,
  optional: SWEEP_INTERVAL_S (default 300), MAX_PER_SWEEP (default 20)
"""
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import requests

# DoH DNS bypass for *.polymarket.com — same trick paper_trade uses, since
# Indian residential DNS hijacks the domain. Harmless on Railway.
import paper_trade  # noqa: F401

from pm_chain import (
    make_w3, load_account, usdc_balance, pusd_balance,
    safe_redeem_positions, safe_wrap_usdce, wait_receipt,
)

# DATABASE_URL is optional — if set, log each sweep to a sweeps table.
DB_URL = os.environ.get("DATABASE_URL", "")
try:
    import psycopg2
except Exception:
    psycopg2 = None

PK = os.environ["LIVE_PRIVATE_KEY"]
RPC = os.environ.get("POLYGON_RPC", "https://1rpc.io/matic")
SAFE = os.environ["LIVE_FUNDER"]
SWEEP_INTERVAL_S = int(os.environ.get("SWEEP_INTERVAL_S", "300"))
MAX_PER_SWEEP = int(os.environ.get("MAX_PER_SWEEP", "20"))
GAS_GWEI_CAP = float(os.environ.get("LIVE_REDEEM_GAS_GW", "300"))
DATA_API = "https://data-api.polymarket.com"

w3 = make_w3(RPC)
acct = load_account(PK)


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def log(msg):
    print(f"{now_utc()}  {msg}", flush=True)


def db_log_sweep(positions_swept: int, redemption_txs: list[str], wrap_txs: tuple,
                 pusd_before: float, pusd_after: float, errors: list[str]) -> None:
    if not DB_URL or psycopg2 is None:
        return
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS claim_sweeps (
                id          BIGSERIAL PRIMARY KEY,
                ts_utc      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                n_redeemed  INTEGER NOT NULL,
                redeem_txs  TEXT[],
                wrap_approve_tx TEXT,
                wrap_tx     TEXT,
                pusd_before NUMERIC(18,6),
                pusd_after  NUMERIC(18,6),
                errors      TEXT[]
            )
        """)
        cur.execute(
            "INSERT INTO claim_sweeps (n_redeemed, redeem_txs, wrap_approve_tx, wrap_tx, pusd_before, pusd_after, errors) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                positions_swept,
                redemption_txs,
                wrap_txs[0] if wrap_txs else None,
                wrap_txs[1] if wrap_txs else None,
                pusd_before, pusd_after,
                errors,
            ),
        )
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        log(f"DB_LOG_ERR  {type(e).__name__}: {e}")


def fetch_redeemable_positions() -> list[dict]:
    url = f"{DATA_API}/positions"
    params = {
        "user": SAFE,
        "limit": MAX_PER_SWEEP,
        "redeemable": "true",
        "sizeThreshold": "0.01",
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json() or []


def redeem_one(pos: dict) -> tuple[str | None, str | None]:
    """Returns (tx_hash, error). On success error is None."""
    cid = pos["conditionId"]
    index_set = 1 << int(pos["outcomeIndex"])  # 0->1 (UP), 1->2 (DOWN)
    short = f"{cid[:10]}.. iset={index_set} sz={pos['size']:.4f}"
    try:
        tx = safe_redeem_positions(
            w3, acct, SAFE, cid, [index_set],
            gas_price_gwei_cap=GAS_GWEI_CAP,
        )
        rcpt = wait_receipt(w3, tx, timeout_s=180)
        if rcpt.get("status") != 1:
            return None, f"REDEEM_FAIL {short} tx={tx} status={rcpt.get('status')}"
        return tx, None
    except Exception as e:
        return None, f"REDEEM_ERR  {short}: {type(e).__name__}: {e}"


def sweep_once() -> None:
    pusd_before = pusd_balance(w3, SAFE)
    usdce_before = usdc_balance(w3, SAFE)
    log(f"SWEEP_BEGIN  Safe pUSD=${pusd_before:.4f} USDC.e=${usdce_before:.4f}")

    positions = fetch_redeemable_positions()
    log(f"SWEEP_FETCH  {len(positions)} redeemable positions")

    redeem_txs = []
    errors = []
    for pos in positions:
        cid_short = pos["conditionId"][:10]
        slug_short = (pos.get("slug") or "")[:48]
        log(f"REDEEM_TRY   cid={cid_short}.. out={pos.get('outcome')} sz={pos['size']:.4f} {slug_short}")
        tx, err = redeem_one(pos)
        if tx:
            redeem_txs.append(tx)
            log(f"REDEEM_OK    tx={tx[:14]}..")
        else:
            errors.append(err or "unknown")
            log(err or "REDEEM_ERR  unknown")

    # After all redemptions, wrap any USDC.e on Safe back to pUSD.
    wrap_result = (None, None)
    try:
        wrap_result = safe_wrap_usdce(w3, acct, SAFE, gas_price_gwei_cap=GAS_GWEI_CAP)
        if wrap_result[0]:
            log(f"WRAP_OK      approve={wrap_result[0][:14]}.. wrap={wrap_result[1][:14]}..")
    except Exception as e:
        msg = f"WRAP_ERR  {type(e).__name__}: {e}"
        log(msg)
        errors.append(msg)

    pusd_after = pusd_balance(w3, SAFE)
    usdce_after = usdc_balance(w3, SAFE)
    delta = pusd_after - pusd_before
    log(f"SWEEP_END    Safe pUSD=${pusd_after:.4f} USDC.e=${usdce_after:.4f}  "
        f"delta=${delta:+.4f}  redeemed={len(redeem_txs)}  errors={len(errors)}")

    db_log_sweep(len(redeem_txs), redeem_txs, wrap_result, pusd_before, pusd_after, errors)


def main() -> None:
    log(f"BOOT  mode=SWEEPER  safe={SAFE}  interval={SWEEP_INTERVAL_S}s  rpc={RPC}")
    while True:
        try:
            sweep_once()
        except Exception:
            log("SWEEP_FATAL  " + traceback.format_exc().replace("\n", " | "))
        time.sleep(SWEEP_INTERVAL_S)


if __name__ == "__main__":
    main()
