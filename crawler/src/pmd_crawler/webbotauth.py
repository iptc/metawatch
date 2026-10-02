"""Web Bot Auth: sign outbound requests so Cloudflare can verify who we are.

Cloudflare's Verified Bots programme accepts a cryptographic signature in
place of a published IP list — the only route open to a crawler running on
GitHub Actions, whose IPs change every run. Each request carries three
headers (RFC 9421 HTTP Message Signatures, profiled by Cloudflare):

    Signature-Agent: "https://iptc.org"
    Signature-Input: sig1=("@authority" "signature-agent");created=…;
                     keyid="<JWK thumbprint>";alg="ed25519";expires=…;
                     nonce="…";tag="web-bot-auth"
    Signature: sig1=:<base64 Ed25519 signature>:

The verifier fetches our public key from
<Signature-Agent>/.well-known/http-message-signatures-directory and checks
the signature over the target host and the Signature-Agent value.

Off unless METAWATCH_WBA_PRIVATE_KEY holds a PEM Ed25519 private key. The
signing headers include a URL-valued non-standard header, which is the kind
of thing that once got us 403'd by TASS's WAF (see DEFAULT_HEADERS), so it
is opt-in per crawl until we've measured its effect.

Reference: https://developers.cloudflare.com/bots/reference/bot-verification/web-bot-auth/
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from collections.abc import Awaitable, Callable

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Origin only: the verifier appends the well-known path itself.
DEFAULT_SIGNATURE_AGENT = "https://iptc.org"
DIRECTORY_PATH = "/.well-known/http-message-signatures-directory"

KEY_ENV = "METAWATCH_WBA_PRIVATE_KEY"
AGENT_ENV = "METAWATCH_WBA_SIGNATURE_AGENT"

# Cloudflare recommends signatures valid for about a minute.
VALIDITY_SECONDS = 60
LABEL = "sig1"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def public_jwk(key: Ed25519PrivateKey) -> dict[str, str]:
    """The public half as an RFC 8037 OKP JWK."""
    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    return {"kty": "OKP", "crv": "Ed25519", "x": _b64url(raw)}


def jwk_thumbprint(jwk: dict[str, str]) -> str:
    """RFC 7638 thumbprint: SHA-256 over the required members, sorted, no
    whitespace. This is the keyid Cloudflare matches against the directory."""
    canonical = json.dumps(
        {k: jwk[k] for k in ("crv", "kty", "x")}, separators=(",", ":"), sort_keys=True,
    )
    return _b64url(hashlib.sha256(canonical.encode("ascii")).digest())


def authority(url: httpx.URL) -> str:
    """RFC 9421 @authority: lowercase host, port only when non-default."""
    host = url.host.lower()
    default = {"http": 80, "https": 443}.get(url.scheme)
    if url.port is not None and url.port != default:
        return f"{host}:{url.port}"
    return host


class Signer:
    def __init__(self, key: Ed25519PrivateKey, signature_agent: str = DEFAULT_SIGNATURE_AGENT):
        self.key = key
        self.keyid = jwk_thumbprint(public_jwk(key))
        # Structured-field string: the quotes are part of the header value,
        # and so part of what gets signed.
        self.agent_header = f'"{signature_agent}"'

    def headers_for(
        self, url: httpx.URL, *, now: int | None = None, nonce: str | None = None,
    ) -> dict[str, str]:
        created = int(time.time()) if now is None else now
        expires = created + VALIDITY_SECONDS
        nonce = nonce if nonce is not None else base64.b64encode(secrets.token_bytes(64)).decode()
        params = (
            '("@authority" "signature-agent")'
            f';created={created};keyid="{self.keyid}";alg="ed25519"'
            f';expires={expires};nonce="{nonce}";tag="web-bot-auth"'
        )
        base = (
            f'"@authority": {authority(url)}\n'
            f'"signature-agent": {self.agent_header}\n'
            f'"@signature-params": {params}'
        )
        sig = base64.b64encode(self.key.sign(base.encode("ascii"))).decode("ascii")
        return {
            "Signature-Agent": self.agent_header,
            "Signature-Input": f"{LABEL}={params}",
            "Signature": f"{LABEL}=:{sig}:",
        }

    async def sign_request(self, request: httpx.Request) -> None:
        # An event hook rather than httpx.Auth: hooks fire on every redirect
        # hop, and each hop may land on a different @authority.
        request.headers.update(self.headers_for(request.url))


def load_signer() -> Signer | None:
    pem = os.environ.get(KEY_ENV, "").strip()
    if not pem:
        return None
    key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit(f"{KEY_ENV} must be an Ed25519 private key")
    agent = os.environ.get(AGENT_ENV, "").strip() or DEFAULT_SIGNATURE_AGENT
    return Signer(key, agent)


def event_hooks(signer: Signer | None) -> dict[str, list[Callable[[httpx.Request], Awaitable[None]]]]:
    """Pass straight to httpx.AsyncClient(event_hooks=…); empty when unsigned."""
    return {"request": [signer.sign_request]} if signer else {}
