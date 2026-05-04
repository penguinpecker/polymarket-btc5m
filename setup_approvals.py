"""
One-time wallet bootstrap script. Run THIS SCRIPT LOCALLY, NOT ON RAILWAY.

Purpose: After funding the bot wallet with USDC.e on Polygon, this sets
the on-chain approvals that Polymarket's CTF Exchange needs to pull
USDC and outcome tokens during trading. Without these, every order
placement will revert.

Approvals set
-------------
1. USDC.e -> CTF Exchange:  approve(MAX_UINT256)   [needed to BUY shares]
2. CTF    -> CTF Exchange:  setApprovalForAll(true) [needed to SELL shares;
                                                      we don't sell, but
                                                      the exchange checks
                                                      this on order match]

Each is set once, lives forever (until manually revoked). Idempotent —
the script reads existing allowance/approval first and skips if already
set to MAX.

Why local-only: this needs your private key. The Railway env will hold
the same PK for trading, but for one-time setup we want you in control
of where the signing happens. Run on your laptop, never paste the PK
into chat / cloud terminals.

Usage
-----
    python -m venv venv && source venv/bin/activate
    pip install -r requirements.txt
    export LIVE_PRIVATE_KEY=0x...your_bot_wallet_pk...
    export POLYGON_RPC=https://polygon-rpc.com   # optional override
    python setup_approvals.py

It will print the wallet address, USDC.e balance, current approvals,
and ask you to type "YES" before sending any tx. ~$0.005 of MATIC per
approval (so ~$0.01 total).
"""
import os
import sys
from web3 import Web3
from eth_account import Account

from pm_chain import (
    USDC_E_ADDR, CTF_ADDR,
    EXCHANGE_V2_ADDR, NEG_RISK_EXCHANGE_V2, NEG_RISK_ADAPTER,
    ERC20_ABI, CTF_ABI, USDC_DECIMALS, CHAIN_ID,
)

MAX_UINT256 = 2**256 - 1

# The CLOB v2 server (post 2026-04-30 migration) checks allowances against
# THREE addresses — the standard v2 exchange, the neg-risk v2 exchange, and
# the neg-risk adapter. Even an EOA-only setup that ONLY trades binary
# (negRisk:false) markets needs all three to make the CLOB report balance>0
# (otherwise every order returns "not enough balance / allowance").
# Source: py-clob-client-v2/issues/32 + on-chain inspection of /balance-allowance.
APPROVAL_TARGETS = [
    ("exchange_v2",          EXCHANGE_V2_ADDR),
    ("neg_risk_exchange_v2", NEG_RISK_EXCHANGE_V2),
    ("neg_risk_adapter",     NEG_RISK_ADAPTER),
]


def main():
    pk = os.environ.get("LIVE_PRIVATE_KEY")
    if not pk:
        print("ERROR: set LIVE_PRIVATE_KEY env var (the bot wallet's PK)")
        sys.exit(1)
    if not pk.startswith("0x"):
        pk = "0x" + pk
    rpc = os.environ.get("POLYGON_RPC", "https://polygon-rpc.com")

    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
    if not w3.is_connected():
        print(f"ERROR: cannot reach RPC {rpc}")
        sys.exit(1)
    if w3.eth.chain_id != CHAIN_ID:
        print(f"ERROR: wrong chain {w3.eth.chain_id}, expected {CHAIN_ID}")
        sys.exit(1)

    acct = Account.from_key(pk)
    addr = acct.address
    print(f"Wallet: {addr}")

    matic = w3.eth.get_balance(addr) / 1e18
    print(f"  MATIC: {matic:.6f}")
    if matic < 0.01:
        print("  WARN: low MATIC. Need ~$0.01 worth for two tx (gas).")

    usdc = w3.eth.contract(address=USDC_E_ADDR, abi=ERC20_ABI)
    usdc_bal = usdc.functions.balanceOf(addr).call() / (10 ** USDC_DECIMALS)
    print(f"  USDC.e: {usdc_bal:.4f}")

    ctf = w3.eth.contract(address=CTF_ADDR, abi=CTF_ABI)

    # Read current state for all three v2 targets.
    state = {}
    print("  USDC.e allowances:")
    for name, target in APPROVAL_TARGETS:
        a = usdc.functions.allowance(addr, target).call()
        state[("usdc", name)] = a
        print(f"    -> {name:24s}: {a / (10**USDC_DECIMALS):,.2f}")
    print("  CTF setApprovalForAll:")
    for name, target in APPROVAL_TARGETS:
        b = ctf.functions.isApprovedForAll(addr, target).call()
        state[("ctf", name)] = b
        print(f"    -> {name:24s}: {b}")

    todo = []
    for name, _target in APPROVAL_TARGETS:
        if state[("usdc", name)] < MAX_UINT256 // 2:
            todo.append(("usdc", name))
        if not state[("ctf", name)]:
            todo.append(("ctf", name))

    if not todo:
        print("\nApprovals already set. Wallet is ready to trade.")
        return

    print(f"\nWill send {len(todo)} tx: {todo}")
    print("Type YES to proceed:")
    confirm = input("> ").strip()
    if confirm != "YES":
        print("aborted")
        sys.exit(0)

    nonce = w3.eth.get_transaction_count(addr)
    gas_price = w3.eth.gas_price
    print(f"  gas_price: {gas_price/1e9:.1f} gwei  nonce_start: {nonce}")

    targets_by_name = {name: target for name, target in APPROVAL_TARGETS}

    for kind, name in todo:
        target = targets_by_name[name]
        if kind == "usdc":
            print(f"\nSending USDC.e approve({name}, MAX_UINT256)...")
            fn = usdc.functions.approve(target, MAX_UINT256)
        else:
            print(f"\nSending CTF setApprovalForAll({name}, true)...")
            fn = ctf.functions.setApprovalForAll(target, True)
        tx = fn.build_transaction({
            "from": addr, "nonce": nonce, "gasPrice": gas_price,
            "chainId": CHAIN_ID,
        })
        try:
            tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.25)
        except Exception:
            tx["gas"] = 80_000
        signed = acct.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
        print(f"  tx: {h}")
        rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=180)
        print(f"  status={rcpt['status']}  gas_used={rcpt['gasUsed']}")
        nonce += 1

    print("\nApprovals set. Wallet ready.")


if __name__ == "__main__":
    main()
