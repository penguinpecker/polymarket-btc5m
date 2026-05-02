"""
End-to-end smoke test of the live mechanics.
Bypasses signal logic, places one $5 FOK BUY on the next BTC-UpDown-5m
market, waits 5 minutes for resolution, calls redeemPositions if won.

Direction defaults to UP. Override:  DIRECTION=DOWN python smoke_test_trade.py
Stake defaults to $5.   Override:  STAKE_USD=2 python smoke_test_trade.py

Run from local venv-live:
    cd ~/polymarket-btc5m
    source venv-live/bin/activate
    python smoke_test_trade.py
"""
import os
import sys
import time
import pathlib
from datetime import datetime, timezone

# Load .env.live before any imports that read env
ENV = pathlib.Path(__file__).parent / ".env.live"
if ENV.exists():
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() and v.strip():
            os.environ.setdefault(k.strip(), v.strip())

# Default to a working RPC (polygon-rpc.com 401s)
os.environ.setdefault("POLYGON_RPC", "https://1rpc.io/matic")

# Imports after env is set. paper_trade applies DoH DNS bypass on import.
from paper_trade import (  # noqa: E402
    fetch_book,
    get_market_by_slug,
    get_polymarket_resolution,
    next_5m_boundary,
)
from pm_clob import make_client, place_market_buy_fok  # noqa: E402
from pm_chain import (  # noqa: E402
    make_w3,
    load_account,
    usdc_balance,
    is_resolved,
    redeem_positions,
    wait_receipt,
)

DIRECTION = os.environ.get("DIRECTION", "UP").upper()
STAKE_USD = float(os.environ.get("STAKE_USD", "5.0"))
PK = os.environ["LIVE_PRIVATE_KEY"]
RPC = os.environ["POLYGON_RPC"]
GAS_GWEI_CAP = float(os.environ.get("LIVE_REDEEM_GAS_GWEI_CAP", "300"))


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"{ts()}  {msg}", flush=True)


def main() -> None:
    assert DIRECTION in ("UP", "DOWN"), f"bad direction {DIRECTION}"
    log(f"=== SMOKE TEST: {DIRECTION}, stake=${STAKE_USD} ===")

    # Pre-flight balance + connectivity
    w3 = make_w3(RPC)
    acct = load_account(PK)
    eoa_usdc = usdc_balance(w3, acct.address)
    matic0 = w3.eth.get_balance(acct.address) / 1e18
    log(f"EOA wallet={acct.address}  USDC.e=${eoa_usdc:.4f}  MATIC={matic0:.4f}")

    # When trading via a Safe/deposit-wallet, real collateral is tracked
    # by CLOB server-side under the funder address (the Safe), not on the
    # EOA. Ask CLOB what it sees.
    funder = os.environ.get("LIVE_FUNDER")
    if funder:
        from pm_clob import make_client
        from py_clob_client_v2 import BalanceAllowanceParams, AssetType
        probe = make_client(
            PK,
            os.environ.get("LIVE_CLOB_API_KEY"),
            os.environ.get("LIVE_CLOB_SECRET"),
            os.environ.get("LIVE_CLOB_PASSPHRASE"),
            signature_type_name=os.environ.get("LIVE_SIGNATURE_TYPE"),
            funder=funder,
        )
        r = probe.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        clob_bal = int(r.get("balance", "0")) / 1_000_000
        log(f"CLOB view: funder={funder} pUSD=${clob_bal:.4f}  allowances={r.get('allowances')}")
        if clob_bal < STAKE_USD:
            log(f"insufficient pUSD per CLOB: ${clob_bal:.4f} < stake ${STAKE_USD}")
            sys.exit(1)
    else:
        if eoa_usdc < STAKE_USD:
            log(f"insufficient USDC.e on EOA: ${eoa_usdc:.4f} < stake ${STAKE_USD}")
            sys.exit(1)

    # Sleep to next 5m boundary (so we have ~4.5 min before window closes)
    now = datetime.now(timezone.utc)
    next_bar = next_5m_boundary(now)
    sl = (next_bar - now).total_seconds() - 1
    log(f"sleeping {sl:.0f}s to boundary {next_bar.strftime('%H:%M:%S')}")
    if sl > 0:
        time.sleep(sl)
    while datetime.now(timezone.utc) < next_bar:
        time.sleep(0.1)
    window_start = int(next_bar.timestamp())

    # Wait T+30s into the window so book is settled
    obs = window_start + 30
    rem = obs - time.time()
    if rem > 0:
        time.sleep(rem)

    # Locate the market
    slug = f"btc-updown-5m-{window_start}"
    m = get_market_by_slug(slug)
    if not m or not (m["token_up"] and m["token_down"]):
        log(f"no market for {slug}")
        sys.exit(2)
    token = m["token_up"] if DIRECTION == "UP" else m["token_down"]
    cid = m.get("condition_id")
    log(f"market={slug}")
    log(f"  cid={cid}")
    log(f"  token={token[:18]}..  side={DIRECTION}")

    # Show the book
    book = fetch_book(token)
    if book and book["asks"]:
        top_ask = book["asks"][0]
        depth = sum(p * s for p, s in book["asks"] if 0.01 < p < 0.99)
        log(f"  book: top_ask={top_ask[0]:.3f} x {top_ask[1]:.0f}  depth=${depth:.0f}")

    # Place FOK BUY
    log(f"placing FOK BUY ${STAKE_USD} on {DIRECTION}...")
    client = make_client(
        PK,
        os.environ.get("LIVE_CLOB_API_KEY"),
        os.environ.get("LIVE_CLOB_SECRET"),
        os.environ.get("LIVE_CLOB_PASSPHRASE"),
        signature_type_name=os.environ.get("LIVE_SIGNATURE_TYPE"),
        funder=os.environ.get("LIVE_FUNDER"),
    )
    t0 = time.time()
    try:
        resp = place_market_buy_fok(client, token, STAKE_USD)
    except Exception as e:
        log(f"ORDER_ERR  {type(e).__name__}: {e}")
        sys.exit(3)
    rt_ms = (time.time() - t0) * 1000
    log(f"  resp ({rt_ms:.0f}ms): {resp}")

    if not resp.get("success"):
        log("ORDER FAILED — see resp above")
        sys.exit(3)
    making = float(resp.get("makingAmount", 0) or 0)
    taking = float(resp.get("takingAmount", 0) or 0)
    order_id = resp.get("orderID") or resp.get("order_id") or ""
    if taking <= 0:
        log(f"NO FILL  resp={resp}")
        sys.exit(4)
    vwap = making / taking
    log(f"FILLED  shares={taking:.4f}  cost=${making:.4f}  vwap={vwap:.4f}  oid={order_id[:12]}..")

    # Wait for window close + 5s
    close_ts = window_start + 300
    wait_s = close_ts + 5 - time.time()
    log(f"waiting {wait_s:.0f}s for window close at {datetime.fromtimestamp(close_ts, tz=timezone.utc).strftime('%H:%M:%S')}")
    if wait_s > 0:
        time.sleep(wait_s)

    # Poll Gamma for resolution (with Binance fallback inside)
    log("polling Polymarket Gamma for resolution...")
    chain_outcome, poll_s = get_polymarket_resolution(slug, timeout_s=300)
    if chain_outcome is None:
        log("RESOLUTION TIMED OUT (Gamma did not resolve in 300s)")
        sys.exit(5)
    log(f"  chain_outcome={chain_outcome:+}  polled_for={poll_s}s")

    pred = +1 if DIRECTION == "UP" else -1
    if chain_outcome == 0:
        log("TIE (refund)")
        sys.exit(0)
    won = (chain_outcome == pred)
    log(f"OUTCOME: {'WIN' if won else 'LOSS'}  (predicted {pred:+}, actual {chain_outcome:+})")

    if not won:
        log(f"final: lost ${making:.4f}, no redeem tx needed (losing tokens are worthless)")
        bal_after = usdc_balance(w3, acct.address)
        log(f"wallet now: USDC.e=${bal_after:.4f}")
        sys.exit(0)

    # WIN — redeem on-chain
    if not cid:
        log("no condition_id; cannot redeem")
        sys.exit(6)
    log("waiting for on-chain resolution to land...")
    deadline = time.time() + 300
    while not is_resolved(w3, cid):
        if time.time() > deadline:
            log("on-chain not resolved after 300s; bailing")
            sys.exit(7)
        log("  not yet on-chain, sleeping 15s")
        time.sleep(15)

    log("on-chain resolved. Sending redeemPositions...")
    bal_before = usdc_balance(w3, acct.address)
    index_set = 1 if DIRECTION == "UP" else 2
    try:
        tx = redeem_positions(w3, acct, cid, [index_set], gas_price_gwei_cap=GAS_GWEI_CAP)
    except Exception as e:
        log(f"REDEEM_ERR  {type(e).__name__}: {e}")
        sys.exit(8)
    log(f"  redeem_tx={tx}")
    rcpt = wait_receipt(w3, tx, timeout_s=180)
    log(f"  receipt status={rcpt.get('status')}  gas_used={rcpt.get('gasUsed')}")
    bal_after = usdc_balance(w3, acct.address)
    received = bal_after - bal_before
    pnl = received - making
    log(f"FINAL  received=${received:.4f}  pnl_vs_stake=${pnl:+.4f}  USDC.e_now=${bal_after:.4f}")


if __name__ == "__main__":
    main()
