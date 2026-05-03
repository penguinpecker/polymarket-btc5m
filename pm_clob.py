"""
CLOB client wrapper — order placement + status, with secrets handling.

Uses py-clob-client-v2 (Polymarket migrated to CLOB v2 on 2026-04-30; the
legacy py-clob-client returns 'order_version_mismatch' on every order).

We use the dedicated EOA bot wallet directly (no signature_type kwarg in
v2; EOA is implicit). The EOA itself holds the USDC.e and outcome tokens.

API creds (L2 HMAC) are server-side records keyed to the wallet address.
They gate the HTTPS API but not on-chain funds. Accept them from env, or
derive them on first boot via create_or_derive_api_key().

Secret hygiene: nothing logged.
"""
import os
import sys

import httpx
import py_clob_client_v2.http_helpers.helpers as _clob_http
from py_clob_client_v2 import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    ClobClient,
    MarketOrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    Side,
    SignatureTypeV2,
)

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137

# Route ALL py-clob-client-v2 HTTP traffic through CLOB_PROXY when set.
# Polymarket geoblocks based on the egress IP; Railway's us-west2 is
# blocked. Set CLOB_PROXY to a SOCKS5/HTTP proxy in an allowed
# jurisdiction (most of EU, UAE, LATAM, India, etc.) to unblock the live
# bot without migrating hosts. Other libraries (web3 RPC, psycopg2,
# requests-based code) are unaffected.
_CLOB_PROXY = os.getenv("CLOB_PROXY")
if _CLOB_PROXY:
    _clob_http._http_client = httpx.Client(http2=True, proxy=_CLOB_PROXY)
    # Mask creds when logging the proxy URL.
    _safe = _CLOB_PROXY
    if "@" in _safe:
        scheme, _, rest = _safe.partition("://")
        _, _, host = rest.rpartition("@")
        _safe = f"{scheme}://***@{host}"
    print(f"[pm_clob] CLOB egress via proxy {_safe}", file=sys.stderr, flush=True)


_SIG_TYPE_BY_NAME = {
    "EOA":              SignatureTypeV2.EOA,
    "POLY_PROXY":       SignatureTypeV2.POLY_PROXY,
    "POLY_GNOSIS_SAFE": SignatureTypeV2.POLY_GNOSIS_SAFE,
    "POLY_1271":        SignatureTypeV2.POLY_1271,
}


def make_client(
    private_key: str,
    api_key: str | None = None,
    api_secret: str | None = None,
    api_passphrase: str | None = None,
    signature_type_name: str | None = None,
    funder: str | None = None,
) -> ClobClient:
    """Build a ClobClient. Returns a fully-authenticated client.

    Polymarket migrated to CLOB v2 on 2026-04-30. v2 collateral is the
    pUSD wrapper, only mintable inside Polymarket's managed deposit flow.
    Pure EOAs cannot trade — the trading wallet is a Gnosis Safe (or
    deposit-wallet 1271 contract) that polymarket.com auto-deploys when
    you onboard. Pass:
      signature_type_name = "POLY_GNOSIS_SAFE" (or "POLY_1271")
      funder              = the Safe / deposit-wallet address (NOT the EOA)

    Default if not specified: EOA — useful only for the legacy v1 markets
    or for read-only queries.
    """
    if not private_key:
        raise RuntimeError("LIVE_PRIVATE_KEY not set")
    pk = private_key if private_key.startswith("0x") else "0x" + private_key

    sig_type = _SIG_TYPE_BY_NAME.get(
        (signature_type_name or "EOA").upper(),
        SignatureTypeV2.EOA,
    )

    common = dict(
        host=CLOB_HOST,
        chain_id=POLYGON_CHAIN_ID,
        key=pk,
        signature_type=sig_type,
    )
    if funder:
        common["funder"] = funder

    if api_key and api_secret and api_passphrase:
        common["creds"] = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )
        return ClobClient(**common)

    # Derive creds via L1 signature, then return fully-authenticated client
    bootstrap = ClobClient(**common)
    common["creds"] = bootstrap.create_or_derive_api_key()
    return ClobClient(**common)


def place_market_buy_fok(
    client: ClobClient, token_id: str, notional_usd: float,
    tick_size: str = "0.01",
) -> dict:
    """Place a marketable BUY — Fill-Or-Kill against current asks.

    `notional_usd` is in USDC. Returns the raw response dict; caller
    should inspect resp.get('success'), resp.get('makingAmount'),
    resp.get('takingAmount').

    BTC-UpDown-5m markets have orderPriceMinTickSize=0.01, hence the
    default. Override if the target market reports a different tick.
    """
    args = MarketOrderArgs(
        token_id=token_id,
        amount=float(notional_usd),
        side=Side.BUY,
        order_type=OrderType.FOK,
    )
    return client.create_and_post_market_order(
        order_args=args,
        options=PartialCreateOrderOptions(tick_size=tick_size),
        order_type=OrderType.FOK,
    )


def get_order(client: ClobClient, order_id: str) -> dict:
    return client.get_order(order_id)


def get_balance_allowance(client: ClobClient) -> dict:
    """USDC.e balance + collateral allowance via CLOB API.
    Useful as a sanity check that complements direct on-chain reads."""
    try:
        return client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
        )
    except Exception as e:
        return {"error": repr(e)}
