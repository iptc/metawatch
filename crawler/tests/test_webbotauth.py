"""Web Bot Auth signing, checked against published known answers.

The Ed25519 key is RFC 9421 Appendix B.1.4's test key, which Cloudflare's
Web Bot Auth docs also use — so its thumbprint must equal the keyid in their
examples, and Ed25519 being deterministic, signing RFC 9421 B.2.6's
signature base must reproduce the RFC's signature byte for byte.
"""

import base64

import httpx
import pytest
from cryptography.hazmat.primitives import serialization

from pmd_crawler import webbotauth
from pmd_crawler.webbotauth import Signer, authority, jwk_thumbprint, public_jwk

RFC9421_ED25519_PEM = """-----BEGIN PRIVATE KEY-----
MC4CAQAwBQYDK2VwBCIEIJ+DYvh6SEqVTm50DFtMDoQikTmiCqirVv9mWG9qfSnF
-----END PRIVATE KEY-----"""


@pytest.fixture
def key():
    return serialization.load_pem_private_key(RFC9421_ED25519_PEM.encode(), password=None)


def test_thumbprint_matches_cloudflare_docs(key):
    jwk = public_jwk(key)
    assert jwk == {"kty": "OKP", "crv": "Ed25519", "x": "JrQLj5P_89iXES9-vFgrIy29clF9CC_oPPsw3c5D0bs"}
    assert jwk_thumbprint(jwk) == "poqkLGiymh_W0uP6PZFw-dvez3QJT5SolqXBCW38r0U"


def test_key_reproduces_rfc9421_b26_signature(key):
    base = (
        '"date": Tue, 20 Apr 2021 02:07:55 GMT\n'
        '"@method": POST\n'
        '"@path": /foo\n'
        '"@authority": example.com\n'
        '"content-type": application/json\n'
        '"content-length": 18\n'
        '"@signature-params": ("date" "@method" "@path" "@authority" "content-type" '
        '"content-length");created=1618884473;keyid="test-key-ed25519"'
    )
    sig = base64.b64encode(key.sign(base.encode())).decode()
    assert sig == "wqcAqbmYJ2ji2glfAMaRy4gruYYnx2nEFN2HN6jrnDnQCK1u02Gb04v9EDgwUPiu4A0w6vuQv5lIp5WPpBKRCw=="


def test_headers_sign_authority_and_agent(key):
    signer = Signer(key, "https://iptc.org")
    h = signer.headers_for(httpx.URL("https://Suspilne.media/rss/all.rss"), now=1_800_000_000, nonce="n0nce")

    assert h["Signature-Agent"] == '"https://iptc.org"'
    params = (
        '("@authority" "signature-agent");created=1800000000'
        ';keyid="poqkLGiymh_W0uP6PZFw-dvez3QJT5SolqXBCW38r0U";alg="ed25519"'
        ';expires=1800000060;nonce="n0nce";tag="web-bot-auth"'
    )
    assert h["Signature-Input"] == f"sig1={params}"

    # Verify against the base a verifier would rebuild from the request.
    base = (
        '"@authority": suspilne.media\n'
        '"signature-agent": "https://iptc.org"\n'
        f'"@signature-params": {params}'
    )
    raw = base64.b64decode(h["Signature"].removeprefix("sig1=:").removesuffix(":"))
    key.public_key().verify(raw, base.encode())  # raises if wrong


@pytest.mark.parametrize("url,expected", [
    ("https://example.com/a", "example.com"),
    ("https://example.com:443/a", "example.com"),
    ("http://example.com:80/a", "example.com"),
    ("https://example.com:8443/a", "example.com:8443"),
    ("https://EXAMPLE.com/a", "example.com"),
])
def test_authority(url, expected):
    assert authority(httpx.URL(url)) == expected


async def test_every_redirect_hop_is_signed_for_its_own_host(key):
    signer = Signer(key)
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers["Signature-Input"]))
        if request.url.host == "tass.ru" and request.url.scheme == "http":
            return httpx.Response(301, headers={"Location": "https://www.tass.ru/rss/v2.xml"})
        return httpx.Response(200, text="ok")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        event_hooks=webbotauth.event_hooks(signer),
    ) as client:
        r = await client.get("http://tass.ru/rss/v2.xml")

    assert r.status_code == 200
    assert [host for host, _ in seen] == ["tass.ru", "www.tass.ru"]
    # Fresh nonce per hop, so the two signatures differ.
    assert seen[0][1] != seen[1][1]


def test_off_without_key(monkeypatch):
    monkeypatch.delenv(webbotauth.KEY_ENV, raising=False)
    assert webbotauth.load_signer() is None
    assert webbotauth.event_hooks(None) == {}


def test_loads_key_and_agent_from_env(monkeypatch):
    monkeypatch.setenv(webbotauth.KEY_ENV, RFC9421_ED25519_PEM)
    monkeypatch.setenv(webbotauth.AGENT_ENV, "https://example.org")
    signer = webbotauth.load_signer()
    assert signer is not None
    assert signer.keyid == "poqkLGiymh_W0uP6PZFw-dvez3QJT5SolqXBCW38r0U"
    assert signer.agent_header == '"https://example.org"'
