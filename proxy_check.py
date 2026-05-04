"""
Verify CLOB egress works (or is geoblocked) WITHOUT placing an order.

Reads CLOB_PROXY + creds from env (or .env.live for local runs), builds
the same ClobClient the live bot uses, and calls two read-only
endpoints:

    1. GET /balance-allowance — exercises auth (L2 HMAC) + the egress
       path Polymarket actually rate-limits per region. Returns balance
       and allowances for the configured collateral.
    2. GET ipinfo.io — reports the country the proxy lands in, so a
       wrong-country proxy fails loudly instead of silently 403'ing.

Exit code 0 = green. Anything else = read the error.

Run from local venv-live (validates the proxy URL before pushing it
into Railway):

    cd ~/polymarket-btc5m
    source venv-live/bin/activate
    CLOB_PROXY=http://user:pass@host:port python proxy_check.py

Run on Railway (validates the live service's egress is unblocked):
    set CLOB_PROXY in Railway env, redeploy, watch logs for the boot
    line — but this script is faster than waiting for the next signal.
"""
import os
import pathlib
import sys

ENV = pathlib.Path(__file__).parent / ".env.live"
if ENV.exists():
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() and v.strip():
            os.environ.setdefault(k.strip(), v.strip())

import httpx  # noqa: E402

# paper_trade applies a socket.getaddrinfo DoH patch for *.polymarket.com
# at import time — needed when the local ISP DNS-blocks the host (Indian
# residential ISPs commonly do this). Harmless on Railway / unblocked
# networks. Import before pm_clob so the patch is live by the time
# httpx hits CLOB.
import paper_trade  # noqa: F401, E402

from pm_clob import make_client, get_balance_allowance  # noqa: E402

PROXY = os.getenv("CLOB_PROXY", "")
PK = os.environ["LIVE_PRIVATE_KEY"]
API_KEY = os.environ.get("LIVE_CLOB_API_KEY", "")
API_SECRET = os.environ.get("LIVE_CLOB_SECRET", "")
API_PASS = os.environ.get("LIVE_CLOB_PASSPHRASE", "")
FUNDER = os.environ.get("LIVE_FUNDER", "")
SIG_TYPE = os.environ.get("LIVE_SIGNATURE_TYPE", "EOA")


def main() -> int:
    print(f"proxy = {'<unset, direct egress>' if not PROXY else 'set (creds masked)'}")
    print(f"sig   = {SIG_TYPE}")
    print(f"funder= {FUNDER or '<eoa-only>'}")

    # 1. Geo-locate the proxy egress (or our direct egress).
    try:
        kw = {"timeout": 10.0}
        if PROXY:
            kw["proxy"] = PROXY
        with httpx.Client(**kw) as c:
            ip_info = c.get("https://ipinfo.io/json").json()
        print(
            f"egress -> {ip_info.get('ip')} "
            f"{ip_info.get('city')}/{ip_info.get('region')}/{ip_info.get('country')} "
            f"({ip_info.get('org')})"
        )
    except Exception as e:
        print(f"ipinfo lookup failed: {e!r}")

    # 2. Hit the actual CLOB endpoint that's behind the geoblock.
    try:
        client = make_client(
            private_key=PK,
            api_key=API_KEY or None,
            api_secret=API_SECRET or None,
            api_passphrase=API_PASS or None,
            signature_type_name=SIG_TYPE,
            funder=FUNDER or None,
        )
    except Exception as e:
        print(f"FAIL  client construct: {e!r}")
        return 2

    ba = get_balance_allowance(client)
    if isinstance(ba, dict) and "error" in ba:
        print(f"FAIL  get_balance_allowance: {ba['error']}")
        if "geoblock" in ba["error"].lower() or "403" in ba["error"]:
            print("      → egress is geoblocked. Use a proxy in an allowed country.")
        return 3

    print(f"OK    get_balance_allowance -> {ba}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
