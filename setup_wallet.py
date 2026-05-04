"""
One-shot wallet + CLOB-creds bootstrap.

Run locally (NOT on Railway). Reads .env.live, fills in any of the four
secrets that are blank, writes back. Order of operations:

  1. If LIVE_PRIVATE_KEY is blank -> generate a fresh EOA and write it.
  2. Use that PK to derive Polymarket CLOB API creds (via SDK signature)
     and write LIVE_CLOB_API_KEY / SECRET / PASSPHRASE.

After it finishes:
  - You'll see the wallet ADDRESS printed (send $100 USDC.e + ~$1 MATIC).
  - .env.live now contains all four secrets.
  - Run setup_approvals.py next, then ./push_live_env.sh.

Idempotent: re-running with a populated .env.live re-uses your PK and
re-derives the same CLOB creds (the SDK signature is deterministic for
the same wallet).
"""
import os
import re
import sys
import time
import socket
import pathlib

# DoH-based DNS bypass for ISPs that hijack polymarket.com lookups.
# Same trick paper_trade.py uses; needed for create_or_derive_api_creds()
# which hits clob.polymarket.com via httpx.
try:
    import requests as _r
    _DOH_CACHE = {}
    def _doh(host):
        hit = _DOH_CACHE.get(host)
        if hit and hit[1] > time.time(): return hit[0]
        try:
            r = _r.get("https://cloudflare-dns.com/dns-query",
                       params={"name": host, "type": "A"},
                       headers={"accept": "application/dns-json"}, timeout=5)
            ips = [a["data"] for a in r.json().get("Answer", []) if a.get("type") == 1]
            if ips:
                _DOH_CACHE[host] = (ips, time.time() + 300)
                return ips
        except Exception:
            pass
        return []
    _orig = socket.getaddrinfo
    def _patched(host, *a, **kw):
        if host and host.endswith("polymarket.com"):
            for ip in _doh(host):
                try: return _orig(ip, *a, **kw)
                except Exception: continue
        return _orig(host, *a, **kw)
    socket.getaddrinfo = _patched
except Exception:
    pass

ENV = pathlib.Path(__file__).parent / ".env.live"


def parse_env(path):
    out = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def write_env(path, kv):
    keys = ["LIVE_PRIVATE_KEY", "LIVE_CLOB_API_KEY", "LIVE_CLOB_SECRET", "LIVE_CLOB_PASSPHRASE"]
    body = (
        "# Live bot secrets — paste values, then run:\n"
        "#   ./push_live_env.sh\n"
        "# That uploads them to Railway via stdin (no shell history) and the file\n"
        "# stays on your local disk only. Delete after pushing if you don't need\n"
        "# it again. Already gitignored (.gitignore: .env, .env.*, *.env).\n\n"
    )
    for k in keys:
        body += f"{k}={kv.get(k, '')}\n"
    path.write_text(body)
    os.chmod(path, 0o600)


def main():
    kv = parse_env(ENV)

    # 1) wallet
    if not kv.get("LIVE_PRIVATE_KEY"):
        try:
            from eth_account import Account
        except ImportError:
            print("ERROR: eth-account not installed. Run:", file=sys.stderr)
            print("  pip install -r requirements.txt", file=sys.stderr)
            sys.exit(1)
        Account.enable_unaudited_hdwallet_features()
        acct = Account.create()
        pk = acct.key.hex()
        if not pk.startswith("0x"):
            pk = "0x" + pk
        kv["LIVE_PRIVATE_KEY"] = pk
        write_env(ENV, kv)
        print(f"  generated new EOA: {acct.address}")
    else:
        try:
            from eth_account import Account
            pk = kv["LIVE_PRIVATE_KEY"]
            if not pk.startswith("0x"):
                pk = "0x" + pk
            acct = Account.from_key(pk)
            print(f"  using existing EOA: {acct.address}")
        except Exception as e:
            print(f"ERROR: invalid LIVE_PRIVATE_KEY in .env.live: {e}", file=sys.stderr)
            sys.exit(1)

    # 2) CLOB creds
    have_clob = all(kv.get(k) for k in ("LIVE_CLOB_API_KEY", "LIVE_CLOB_SECRET", "LIVE_CLOB_PASSPHRASE"))
    if have_clob:
        print("  CLOB creds already present in .env.live, skipping derivation")
    else:
        try:
            from py_clob_client_v2 import ClobClient
        except ImportError:
            print("ERROR: py-clob-client-v2 not installed. Run:", file=sys.stderr)
            print("  pip install -r requirements.txt", file=sys.stderr)
            sys.exit(1)
        pk = kv["LIVE_PRIVATE_KEY"]
        if not pk.startswith("0x"):
            pk = "0x" + pk
        print("  deriving CLOB API credentials (signs a message with your PK)...")
        c = ClobClient(host="https://clob.polymarket.com", chain_id=137, key=pk)
        creds = c.create_or_derive_api_key()
        kv["LIVE_CLOB_API_KEY"]    = creds.api_key
        kv["LIVE_CLOB_SECRET"]     = creds.api_secret
        kv["LIVE_CLOB_PASSPHRASE"] = creds.api_passphrase
        write_env(ENV, kv)
        print("  wrote CLOB creds to .env.live")

    # final summary
    print()
    print(f"  wallet address: {acct.address}")
    print()
    print("Next steps:")
    print(f"  1. Send $100 USDC.e + ~$1 MATIC (Polygon, chain 137) to the address above.")
    print(f"     USDC.e contract: 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
    print(f"  2. After funds arrive, run: python setup_approvals.py")
    print(f"  3. Push secrets to Railway:  ./push_live_env.sh")


if __name__ == "__main__":
    main()
