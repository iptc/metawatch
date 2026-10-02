"""Check a deployed Web Bot Auth key directory the way a verifier would.

    .venv/bin/python crawler/scripts/wba_check_directory.py
    .venv/bin/python crawler/scripts/wba_check_directory.py --keyid <expected>
    # against a local test server, presenting the production Host:
    .venv/bin/python crawler/scripts/wba_check_directory.py \
        --url http://localhost:8080/.well-known/http-message-signatures-directory --host iptc.org

Fetches the directory, then checks: content type, JWKS shape, that the
response is signed by the key it publishes (rebuilding the RFC 9421 signature
base independently of the PHP that produced it), that keyid is that key's
thumbprint, and that created/expires bracket now. Exits non-zero on failure.
"""

import argparse
import base64
import re
import sys
import time

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from pmd_crawler.webbotauth import DEFAULT_SIGNATURE_AGENT, DIRECTORY_PATH, jwk_thumbprint

CONTENT_TYPE = "application/http-message-signatures-directory+json"


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_SIGNATURE_AGENT + DIRECTORY_PATH)
    parser.add_argument("--host", help="Host header to send (and expect signed); defaults to the URL's host.")
    parser.add_argument("--keyid", help="Fail unless the directory publishes this keyid.")
    args = parser.parse_args()

    url = httpx.URL(args.url)
    host = (args.host or url.host).lower()
    r = httpx.get(url, headers={"Host": host}, timeout=15)

    problems: list[str] = []

    def check(ok: bool, msg: str) -> None:
        print(("  ok    " if ok else "  FAIL  ") + msg)
        if not ok:
            problems.append(msg)

    print(f"GET {url}  (Host: {host})  →  {r.status_code}")
    check(r.status_code == 200, "status 200")
    ctype = r.headers.get("content-type", "").split(";")[0].strip()
    check(ctype == CONTENT_TYPE, f"content-type {ctype!r}")

    try:
        keys = r.json()["keys"]
        jwk = keys[0]
        check(len(keys) == 1 and jwk.get("kty") == "OKP" and jwk.get("crv") == "Ed25519",
              "JWKS holds one Ed25519 key")
    except (ValueError, KeyError, IndexError) as e:
        check(False, f"JWKS parse: {e}")
        sys.exit(1)
    thumb = jwk_thumbprint(jwk)

    sig_input = r.headers.get("signature-input", "")
    sig_header = r.headers.get("signature", "")
    m = re.fullmatch(r"(\w+)=(.+)", sig_input)
    s = re.fullmatch(r"(\w+)=:([A-Za-z0-9+/=]+):", sig_header)
    check(bool(m and s and m.group(1) == s.group(1)), "Signature / Signature-Input present, same label")
    if not (m and s):
        sys.exit(1)
    params = m.group(2)

    def param(name: str) -> str | None:
        p = re.search(rf';{name}=("([^"]*)"|(\d+))', params)
        return (p.group(2) if p.group(2) is not None else p.group(3)) if p else None

    check(params.startswith('("@authority";req)'), "covers @authority;req")
    check(param("tag") == "http-message-signatures-directory", f"tag {param('tag')!r}")
    check(param("alg") == "ed25519", f"alg {param('alg')!r}")
    check(param("keyid") == thumb, f"keyid is the published key's thumbprint ({thumb})")
    if args.keyid:
        check(thumb == args.keyid, f"published key is the expected one ({args.keyid})")
    now = int(time.time())
    created, expires = int(param("created") or 0), int(param("expires") or 0)
    check(created - 5 <= now <= expires, f"now within created..expires ({expires - created}s window)")

    base = f'"@authority";req: {host}\n"@signature-params": {params}'
    try:
        Ed25519PublicKey.from_public_bytes(b64url_decode(jwk["x"])).verify(
            base64.b64decode(s.group(2)), base.encode("ascii"),
        )
        check(True, "signature verifies against the published key")
    except Exception as e:  # noqa: BLE001 — any failure here is the answer
        check(False, f"signature verifies against the published key ({type(e).__name__})")

    print("\nPASS" if not problems else f"\n{len(problems)} problem(s)")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
