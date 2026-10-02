"""no_feed_found: every feed/sitemap address we tried simply wasn't there.

Modelled on pap.pl (2026-10): no feed or sitemap, guessed paths 404 behind
Imperva (which injects its script into the 404 pages), and /rss.xml is a soft
404 — an HTML "Not found." page served with 200. That used to surface as
blocked_by_waf or discovery_blocked, neither of which is true.
"""

import httpx
import pytest

from pmd_crawler import discovery
from pmd_crawler.config import DiscoveryConfig, SampleConfig, Site
from pmd_crawler.discovery import (
    _not_found_kind,
    status_for_empty_discovery,
    summarise_discovery_errors,
)

HTML = {"content-type": "text/html; charset=UTF-8"}
IMPERVA = {**HTML, "x-cdn": "Imperva", "x-iinfo": "45-1"}
INCAPSULA_404 = (b'<html><head><title>Strona nieznaleziona</title>'
                 b'<script src="/_Incapsula_Resource?SWJIYLWA=5074"></script></head></html>')


def _site(**discovery_kwargs) -> Site:
    return Site(
        id="x", name="X", url="https://x.example/", country="XX",
        homepage="https://x.example/",
        discovery=DiscoveryConfig(**discovery_kwargs),
        sample=SampleConfig(),
    )


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(discovery, "_RSS_RETRY_BACKOFF_S", 0)


async def _discover(site: Site, handler) -> str:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        _robots, _url, strategy, articles, err = await discovery.discover(client, site)
    assert articles == []
    return status_for_empty_discovery(strategy, err)


def test_not_found_kind():
    assert _not_found_kind(httpx.Response(404)) == "not_found"
    assert _not_found_kind(httpx.Response(410)) == "not_found"
    assert _not_found_kind(httpx.Response(200, headers=HTML, content=b"<html>Not found.</html>")) == "not_found"
    assert _not_found_kind(httpx.Response(403, headers=HTML)) is None
    assert _not_found_kind(httpx.Response(200, headers={"content-type": "application/xml"})) is None


@pytest.mark.parametrize("errors,expected", [
    ([], None),
    ([None], None),
    (["not_found", "not_found"], "not_found"),
    # A real refusal anywhere outranks the 404s that followed it.
    (["waf:cloudflare", "not_found", "not_found"], "waf:cloudflare"),
    (["not_found", "http_error", "not_found"], "http_error"),
    # Among real errors, the last still wins (unchanged behaviour).
    (["http_error", "parse_error", "not_found"], "parse_error"),
])
def test_summarise(errors, expected):
    assert summarise_discovery_errors(errors) == expected


def test_status_mapping():
    assert status_for_empty_discovery("no_sitemap", "not_found") == "no_feed_found"
    assert status_for_empty_discovery("no_sitemap", "http_error") == "discovery_blocked"


async def test_pap_like_site_is_no_feed_found():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /search/\n")
        if path == "/":
            return httpx.Response(200, headers=HTML, content=b"<html><head></head><body>News</body></html>")
        if path == "/rss.xml":
            return httpx.Response(200, headers=HTML, content=b"<html><body>Not found.</body></html>")
        return httpx.Response(404, headers=IMPERVA, content=INCAPSULA_404)

    assert await _discover(_site(), handler) == "no_feed_found"
    # A 404 isn't retried (http_error is), so each guessed path is asked once.
    feed_paths = [p for p in seen if p not in ("/", "/robots.txt")]
    assert len(feed_paths) == len(set(feed_paths))


async def test_configured_feed_refusal_is_not_masked():
    # Configured feed refused with a Cloudflare challenge; the homepage scan
    # then finds nothing. Still a block, not "no feed".
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/feed":
            return httpx.Response(403, headers={**HTML, "cf-ray": "8a1b"},
                                  content=b"<title>Just a moment...</title>")
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/":
            return httpx.Response(200, headers=HTML, content=b"<html></html>")
        return httpx.Response(404)

    status = await _discover(_site(rss_urls=["https://x.example/feed"]), handler)
    assert status == "blocked_by_waf"


async def test_malformed_xml_feed_stays_parse_error():
    # Served as XML but broken: the publisher's feed exists, it's just bad.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/feed":
            return httpx.Response(200, headers={"content-type": "application/rss+xml"},
                                  content=b"<?xml version='1.0'?>\n<<< not a feed >>>")
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/":
            return httpx.Response(200, headers=HTML, content=b"<html></html>")
        return httpx.Response(404)

    status = await _discover(_site(rss_urls=["https://x.example/feed"]), handler)
    assert status == "discovery_parse_error"
