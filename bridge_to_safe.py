"""
Bridge USDC.e from the EOA to the Polymarket Safe (mints pUSD on the Safe)
via the Relay API — same path polymarket.com's Deposit button uses.

Two txs total:
  1. approve(USDC.e -> Polymarket onboarding router, amount)
  2. router.transferAndMulticall(...)  — Relay pre-builds the calldata,
     including the signature payload we don't have to construct.

Usage:
  AMOUNT_USDC=1.0 python bridge_to_safe.py
"""
import os
import sys
import json
import time
import pathlib
import urllib.request

ENV = pathlib.Path(__file__).parent / ".env.live"
if ENV.exists():
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() and v.strip():
            os.environ.setdefault(k.strip(), v.strip())

os.environ.setdefault("POLYGON_RPC", "https://1rpc.io/matic")

from web3 import Web3
from eth_account import Account

USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PUSD   = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

PK     = os.environ["LIVE_PRIVATE_KEY"]
SAFE   = os.environ["LIVE_FUNDER"]
RPC    = os.environ["POLYGON_RPC"]
AMOUNT = float(os.environ.get("AMOUNT_USDC", "1.0"))

ABI20 = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}, {"type": "address"}],
     "outputs": [{"type": "uint256"}]},
]


def log(msg: str) -> None:
    print(f"[bridge] {msg}", flush=True)


def quote(user: str, recipient: str, amount_micro: int) -> dict:
    body = json.dumps({
        "user": user,
        "recipient": recipient,
        "originChainId": 137,
        "destinationChainId": 137,
        "originCurrency": USDC_E,
        "destinationCurrency": PUSD,
        "amount": str(amount_micro),
        "tradeType": "EXACT_INPUT",
    }).encode()
    req = urllib.request.Request(
        "https://api.relay.link/quote",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def send_tx(w3: Web3, acct, tx_data: dict, label: str) -> str:
    tx = {
        "from":     acct.address,
        "to":       Web3.to_checksum_address(tx_data["to"]),
        "data":     tx_data["data"],
        "value":    int(tx_data.get("value", "0")),
        "chainId":  137,
        "nonce":    w3.eth.get_transaction_count(acct.address),
        "gasPrice": w3.eth.gas_price,
    }
    try:
        est = w3.eth.estimate_gas(tx)
        tx["gas"] = int(est * 1.20)
    except Exception:
        tx["gas"] = int(tx_data.get("gas", 600_000))

    log(f"{label} pre-sign: nonce={tx['nonce']} gasPrice={tx['gasPrice']/1e9:.1f} gw  gas={tx['gas']}")
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
    log(f"{label} tx: {h}")
    rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    log(f"{label} status={rcpt['status']}  gas_used={rcpt['gasUsed']}")
    if rcpt["status"] != 1:
        raise RuntimeError(f"{label} reverted")
    return h


def main():
    if AMOUNT <= 0:
        log("AMOUNT_USDC must be > 0")
        sys.exit(1)

    w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 15}))
    pk = PK if PK.startswith("0x") else "0x" + PK
    acct = Account.from_key(pk)

    amount_micro = int(round(AMOUNT * 1_000_000))
    log(f"EOA       : {acct.address}")
    log(f"Safe      : {SAFE}")
    log(f"Amount    : ${AMOUNT:.4f} USDC.e ({amount_micro} micro)")

    # Pre-flight balances
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=ABI20)
    pusd = w3.eth.contract(address=Web3.to_checksum_address(PUSD), abi=ABI20)
    eoa_usdc = usdc.functions.balanceOf(acct.address).call()
    safe_pusd_before = pusd.functions.balanceOf(Web3.to_checksum_address(SAFE)).call()
    log(f"EOA USDC.e: ${eoa_usdc/1e6:.4f}  Safe pUSD: ${safe_pusd_before/1e6:.4f}")
    if eoa_usdc < amount_micro:
        log(f"insufficient USDC.e on EOA")
        sys.exit(1)
    matic = w3.eth.get_balance(acct.address)
    log(f"MATIC     : {matic/1e18:.4f}")

    log("requesting Relay quote...")
    q = quote(acct.address, SAFE, amount_micro)
    steps = q.get("steps", [])
    if not steps:
        log(f"no steps in quote: {json.dumps(q)[:500]}")
        sys.exit(2)

    fees = q.get("fees", {})
    log(f"quote: steps={[s['id'] for s in steps]}  "
        f"app_fee={fees.get('app',{}).get('amountUsd','?')}  "
        f"gas_est_usd={fees.get('gas',{}).get('amountUsd','?')}  "
        f"relayer_fee_usd={fees.get('relayer',{}).get('amountUsd','?')}")

    # Execute each step's transactions in order
    for step in steps:
        for item in step.get("items", []):
            if item.get("status") == "complete":
                continue
            send_tx(w3, acct, item["data"], step["id"])

    # Final balances
    eoa_after = usdc.functions.balanceOf(acct.address).call() / 1e6
    safe_after = pusd.functions.balanceOf(Web3.to_checksum_address(SAFE)).call() / 1e6
    log(f"AFTER:  EOA USDC.e=${eoa_after:.4f}  Safe pUSD=${safe_after:.4f}")
    log(f"        delta_eoa={(eoa_after - eoa_usdc/1e6):+.4f}  delta_safe={(safe_after - safe_pusd_before/1e6):+.4f}")


if __name__ == "__main__":
    main()
