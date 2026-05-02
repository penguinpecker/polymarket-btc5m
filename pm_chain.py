"""
On-chain helpers for the live bot — read balances, redeem winning positions.

Verified 2026-05-02 against Polymarket/neg-risk-ctf-adapter/addresses.json
(chain 137) and a sample resolved BTC-UpDown-5m market (negRisk: false).

BTC-UpDown-5m markets are STANDARD binary CTF — they DO NOT use the
NegRiskAdapter. Trades route through the CTF Exchange and redemption
calls ConditionalTokens.redeemPositions directly. The NegRisk addresses
are kept for reference only and are unused on this code path.

Secrets handling: PRIVATE_KEY is read from env once and used to derive
the address; the raw key is never logged or persisted to disk. eth_account
holds it in memory.
"""
import os
from web3 import Web3
from eth_account import Account

# Polygon mainnet (chain 137)
USDC_E_ADDR       = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
CTF_ADDR          = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
# v1 CTF Exchange (deprecated by Polymarket on 2026-04-30 with the CLOB v2
# migration). Kept for redemption ABI compatibility (the redeemPositions
# call on the CTF contract itself didn't change).
CTF_EXCHANGE_ADDR = Web3.to_checksum_address("0x4bFb41d5B3570DEFd03C39a9A4D8dE6Bd8B8982E")
# v2 contracts — required after the 2026-04-30 cutover. The CLOB's
# balance/allowance check returns balance=0 for accounts without
# approvals to these. Source: py-clob-client-v2/issues/32.
EXCHANGE_V2_ADDR        = Web3.to_checksum_address("0xE111180000d2663C0091e4f400237545B87B996B")
NEG_RISK_EXCHANGE_V2    = Web3.to_checksum_address("0xe2222d279d744050d28e00520010520000310F59")
# Used for redemption on NegRisk markets (BTC-UpDown-5m is NOT NegRisk so
# we don't currently redeem through this; kept for completeness).
NEG_RISK_ADAPTER  = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")

CHAIN_ID = 137

ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [],
     "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"},
                {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]

CTF_ABI = [
    # ERC1155-style balance for outcome tokens. positionId is the uint256 token id.
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"},
                {"name": "id", "type": "uint256"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "isApprovedForAll", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"},
                {"name": "operator", "type": "address"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "setApprovalForAll", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "operator", "type": "address"},
                {"name": "approved", "type": "bool"}],
     "outputs": []},
    # Resolution status — payoutDenominator(conditionId) is non-zero iff resolved.
    {"name": "payoutDenominator", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "conditionId", "type": "bytes32"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "payoutNumerators", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "conditionId", "type": "bytes32"},
                {"name": "index", "type": "uint256"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    # Burn outcome tokens, receive collateral pro-rata to payout numerators.
    # Standard Gnosis CTF signature.
    {"name": "redeemPositions", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "collateralToken", "type": "address"},
                {"name": "parentCollectionId", "type": "bytes32"},
                {"name": "conditionId", "type": "bytes32"},
                {"name": "indexSets", "type": "uint256[]"}],
     "outputs": []},
]

USDC_DECIMALS = 6

# pUSD = Polymarket USD wrapper (v2 collateral). USDC.e is wrapped 1:1
# during settlement. Direct user balance checks should query this when
# the wallet is a Polymarket-managed Safe.
PUSD_ADDR = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")

# Gnosis Safe v1.3.0 ABI (minimal — only what we need for redeem).
SAFE_ABI = [
    {"name": "nonce", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint256"}]},
    {"name": "getOwners", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "address[]"}]},
    {"name": "getThreshold", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint256"}]},
    {"name": "VERSION", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "string"}]},
    {"name": "getTransactionHash", "type": "function", "stateMutability": "view",
     "inputs": [
         {"name": "to", "type": "address"},
         {"name": "value", "type": "uint256"},
         {"name": "data", "type": "bytes"},
         {"name": "operation", "type": "uint8"},
         {"name": "safeTxGas", "type": "uint256"},
         {"name": "baseGas", "type": "uint256"},
         {"name": "gasPrice", "type": "uint256"},
         {"name": "gasToken", "type": "address"},
         {"name": "refundReceiver", "type": "address"},
         {"name": "_nonce", "type": "uint256"},
     ],
     "outputs": [{"type": "bytes32"}]},
    {"name": "execTransaction", "type": "function", "stateMutability": "payable",
     "inputs": [
         {"name": "to", "type": "address"},
         {"name": "value", "type": "uint256"},
         {"name": "data", "type": "bytes"},
         {"name": "operation", "type": "uint8"},
         {"name": "safeTxGas", "type": "uint256"},
         {"name": "baseGas", "type": "uint256"},
         {"name": "gasPrice", "type": "uint256"},
         {"name": "gasToken", "type": "address"},
         {"name": "refundReceiver", "type": "address"},
         {"name": "signatures", "type": "bytes"},
     ],
     "outputs": [{"type": "bool"}]},
]

ZERO_ADDR = "0x0000000000000000000000000000000000000000"


def make_w3(rpc_url: str) -> Web3:
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
    if not w3.is_connected():
        raise RuntimeError(f"web3: cannot reach RPC {rpc_url}")
    cid = w3.eth.chain_id
    if cid != CHAIN_ID:
        raise RuntimeError(f"web3: wrong chain {cid}, expected {CHAIN_ID}")
    return w3


def load_account(private_key: str) -> Account:
    if not private_key:
        raise RuntimeError("LIVE_PRIVATE_KEY not set")
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key
    return Account.from_key(private_key)


def usdc_balance(w3: Web3, owner: str) -> float:
    c = w3.eth.contract(address=USDC_E_ADDR, abi=ERC20_ABI)
    raw = c.functions.balanceOf(Web3.to_checksum_address(owner)).call()
    return raw / (10 ** USDC_DECIMALS)


def pusd_balance(w3: Web3, owner: str) -> float:
    c = w3.eth.contract(address=PUSD_ADDR, abi=ERC20_ABI)
    raw = c.functions.balanceOf(Web3.to_checksum_address(owner)).call()
    return raw / (10 ** USDC_DECIMALS)


def usdc_allowance(w3: Web3, owner: str, spender: str) -> float:
    c = w3.eth.contract(address=USDC_E_ADDR, abi=ERC20_ABI)
    raw = c.functions.allowance(
        Web3.to_checksum_address(owner),
        Web3.to_checksum_address(spender),
    ).call()
    return raw / (10 ** USDC_DECIMALS)


def ctf_token_balance(w3: Web3, owner: str, token_id: int) -> int:
    c = w3.eth.contract(address=CTF_ADDR, abi=CTF_ABI)
    return c.functions.balanceOf(Web3.to_checksum_address(owner), int(token_id)).call()


def is_resolved(w3: Web3, condition_id: str) -> bool:
    c = w3.eth.contract(address=CTF_ADDR, abi=CTF_ABI)
    cid = condition_id if condition_id.startswith("0x") else "0x" + condition_id
    return c.functions.payoutDenominator(bytes.fromhex(cid[2:])).call() > 0


def payout_numerators(w3: Web3, condition_id: str) -> tuple[int, int]:
    """Return (num_up, num_down) for a binary market. Both 0 until resolved."""
    c = w3.eth.contract(address=CTF_ADDR, abi=CTF_ABI)
    cid = condition_id if condition_id.startswith("0x") else "0x" + condition_id
    cb = bytes.fromhex(cid[2:])
    n0 = c.functions.payoutNumerators(cb, 0).call()
    n1 = c.functions.payoutNumerators(cb, 1).call()
    return n0, n1


def redeem_positions(
    w3: Web3, account, condition_id: str, index_sets: list[int],
    gas_price_gwei_cap: float = 200.0,
) -> str:
    """Burn outcome tokens for `condition_id`, receive USDC.e payout.

    `index_sets` is the bitmask list — for our binary markets:
      [1] = redeem UP-token holdings, [2] = redeem DOWN-token holdings.
    Pass only the side we own — passing both with zero balance on one is
    fine (no-op for that side) but wastes a tiny bit of gas.

    Returns the tx hash hex string. Caller should poll for receipt.
    """
    c = w3.eth.contract(address=CTF_ADDR, abi=CTF_ABI)
    cid = condition_id if condition_id.startswith("0x") else "0x" + condition_id
    cb = bytes.fromhex(cid[2:])

    nonce = w3.eth.get_transaction_count(account.address)
    gas_price = w3.eth.gas_price
    cap = int(gas_price_gwei_cap * 1e9)
    if gas_price > cap:
        raise RuntimeError(f"gas_price {gas_price/1e9:.1f} gwei exceeds cap {gas_price_gwei_cap}")

    tx = c.functions.redeemPositions(
        USDC_E_ADDR, b"\x00" * 32, cb, [int(x) for x in index_sets],
    ).build_transaction({
        "from": account.address,
        "nonce": nonce,
        "gasPrice": gas_price,
        "chainId": CHAIN_ID,
    })
    # Estimate then pad 25%
    try:
        est = w3.eth.estimate_gas(tx)
        tx["gas"] = int(est * 1.25)
    except Exception:
        tx["gas"] = 250_000

    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash.hex()


def wait_receipt(w3: Web3, tx_hash: str, timeout_s: int = 180) -> dict:
    return dict(w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout_s))


def safe_redeem_positions(
    w3: Web3, account, safe_address: str, condition_id: str, index_sets: list[int],
    gas_price_gwei_cap: float = 300.0,
) -> str:
    """Redeem outcome tokens held by a Gnosis Safe (v1.3.0) via execTransaction.

    For Polymarket v2: the trading wallet is a Safe owned by the EOA.
    The Safe holds the outcome tokens (CTF ERC1155) and pUSD; redemption
    must be invoked AS the Safe. Single-owner threshold=1 Safes accept
    a 65-byte ECDSA signature from the owner directly via execTransaction.

    Returns tx hash of the execTransaction call (the inner redeemPositions
    is wrapped by the Safe). Caller polls receipt for status.
    """
    safe = w3.eth.contract(address=Web3.to_checksum_address(safe_address), abi=SAFE_ABI)
    ctf  = w3.eth.contract(address=CTF_ADDR, abi=CTF_ABI)

    cid = condition_id if condition_id.startswith("0x") else "0x" + condition_id
    cb = bytes.fromhex(cid[2:])

    # Inner call: CTF.redeemPositions(USDC.e, parent=0x0, conditionId, indexSets)
    inner_data = ctf.encode_abi(
        abi_element_identifier="redeemPositions",
        args=[USDC_E_ADDR, b"\x00" * 32, cb, [int(x) for x in index_sets]],
    )

    safe_nonce = safe.functions.nonce().call()
    safe_tx_hash_bytes = safe.functions.getTransactionHash(
        CTF_ADDR,    # to
        0,           # value
        inner_data,  # data
        0,           # operation: CALL
        0,           # safeTxGas (0 = unlimited within tx gas)
        0,           # baseGas
        0,           # gasPrice (0 = no Safe-level refund)
        ZERO_ADDR,   # gasToken
        ZERO_ADDR,   # refundReceiver
        safe_nonce,
    ).call()

    # ECDSA-sign the SafeTxHash with the EOA owner. eth_account 0.13+ uses
    # unsafe_sign_hash; older releases used signHash. Try both.
    try:
        signed = account.unsafe_sign_hash(safe_tx_hash_bytes)
    except AttributeError:
        signed = account.signHash(safe_tx_hash_bytes)
    sig_bytes = signed.signature  # 65 bytes: r || s || v (v in {27,28})

    # Build + send execTransaction from the EOA (gas paid by EOA in MATIC)
    nonce = w3.eth.get_transaction_count(account.address)
    gas_price = w3.eth.gas_price
    cap = int(gas_price_gwei_cap * 1e9)
    if gas_price > cap:
        raise RuntimeError(f"gas_price {gas_price/1e9:.1f} gwei exceeds cap {gas_price_gwei_cap}")

    tx = safe.functions.execTransaction(
        CTF_ADDR, 0, inner_data, 0, 0, 0, 0, ZERO_ADDR, ZERO_ADDR, sig_bytes,
    ).build_transaction({
        "from": account.address,
        "nonce": nonce,
        "gasPrice": gas_price,
        "chainId": CHAIN_ID,
    })
    try:
        est = w3.eth.estimate_gas(tx)
        tx["gas"] = int(est * 1.25)
    except Exception:
        tx["gas"] = 350_000

    signed_tx = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    return tx_hash.hex()
