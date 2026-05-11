"""CDN attribution from response headers and image URL hosts.

See SPEC.md §5b. Three signals:
  1. Response headers (provider-specific).
  2. Image URL host pattern.
  3. CNAME lookup of the image host (cached per-host per-run).

The CNAME step is intentionally last and cheap-on-miss: we only resolve when
headers and URL patterns are inconclusive.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from urllib.parse import urlparse

HEADER_RULES: list[tuple[str, str, str | None]] = [
    # (provider, header_name, optimizer_signal_substring | None)
    ("cloudflare", "cf-ray", None),
    ("cloudflare", "cf-polish", "1"),
    ("cloudfront", "x-amz-cf-id", None),
    ("fastly", "x-served-by", "cache-"),
    ("fastly", "x-fastly-request-id", None),
    ("akamai", "x-akamai-transformed", "*"),
    ("akamai", "x-akamai-request-id", None),
    ("imgix", "x-imgix-id", None),
    ("cloudinary", "x-cld-cache", None),
    ("bunny", "cdn-pullzone", None),
]

# (provider, hostname substring)
HOST_RULES: list[tuple[str, str]] = [
    ("imgix", ".imgix.net"),
    ("cloudinary", ".cloudinary.com"),
    ("cloudfront", ".cloudfront.net"),
    ("akamai", ".akamaized.net"),
    ("akamai", ".akamaihd.net"),
    ("fastly", ".fastly.net"),
    ("fastly", ".fastlylb.net"),
    ("bunny", ".b-cdn.net"),
    ("bunny", ".bunnycdn.com"),
    ("cloudflare", ".cdn.cloudflare.net"),
]

CNAME_RULES: list[tuple[str, str]] = [
    ("fastly", "fastly.net"),
    ("fastly", "fastlylb.net"),
    ("akamai", "akamaiedge.net"),
    ("akamai", "akamai.net"),
    ("akamai", "edgekey.net"),
    ("cloudfront", "cloudfront.net"),
    ("cloudflare", "cloudflare.net"),
    ("imgix", "imgix.net"),
    ("cloudinary", "cloudinary.com"),
    ("bunny", "b-cdn.net"),
]


@dataclass
class CdnResult:
    provider: str
    optimizer_active: str  # "true" | "false" | "unknown"


_CNAME_CACHE: dict[str, str | None] = {}


def detect(
    image_url: str, response_headers: dict[str, str] | None
) -> CdnResult:
    headers = {k.lower(): v for k, v in (response_headers or {}).items()}

    provider_from_header: str | None = None
    optimizer_evidence = False
    for provider, header_name, opt_signal in HEADER_RULES:
        if header_name in headers:
            if provider_from_header is None:
                provider_from_header = provider
            if opt_signal is not None:
                value = headers[header_name]
                if opt_signal == "*" or opt_signal in value:
                    optimizer_evidence = True

    host = urlparse(image_url).hostname or ""
    provider_from_host: str | None = None
    for provider, needle in HOST_RULES:
        if needle in host:
            provider_from_host = provider
            break

    # Cloudinary delivery URLs with `f_auto` / `q_auto` query/path = active optimization
    if "/upload/" in image_url and ("f_auto" in image_url or "q_auto" in image_url):
        optimizer_evidence = True
    if "imgix.net" in host and ("auto=" in image_url or "fm=" in image_url):
        optimizer_evidence = True

    provider = provider_from_header or provider_from_host
    if provider is None and host:
        cname_provider = _cname_lookup(host)
        if cname_provider:
            provider = cname_provider

    if provider is None:
        return CdnResult("none", "unknown")

    if optimizer_evidence:
        return CdnResult(provider, "true")

    return CdnResult(provider, "false" if provider_from_header else "unknown")


def _cname_lookup(host: str) -> str | None:
    if host in _CNAME_CACHE:
        cached = _CNAME_CACHE[host]
        return cached
    try:
        info = socket.gethostbyname_ex(host)
        chain = " ".join(info[1]) + " " + info[0]
    except Exception:
        _CNAME_CACHE[host] = None
        return None
    for provider, needle in CNAME_RULES:
        if needle in chain:
            _CNAME_CACHE[host] = provider
            return provider
    _CNAME_CACHE[host] = None
    return None
