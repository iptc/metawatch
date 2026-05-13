"""Sanity-check that the sitemap fetch path can decompress .xml.gz responses.

We don't hit the network — instead patch httpx.AsyncClient.get to return a
canned gzipped sitemap and check that _walk_sitemap parses it.
"""
import asyncio
import gzip
from unittest.mock import AsyncMock, MagicMock

from pmd_crawler.discovery import _walk_sitemap

SITEMAP_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/article-1</loc></url>
  <url><loc>https://example.com/article-2</loc></url>
</urlset>
"""


def _fake_client(body: bytes, status: int = 200):
    client = MagicMock()
    response = MagicMock()
    response.status_code = status
    response.content = body
    client.get = AsyncMock(return_value=response)
    return client


def test_gzipped_sitemap_by_url_suffix():
    body = gzip.compress(SITEMAP_XML)
    client = _fake_client(body)
    candidates, err = asyncio.run(
        _walk_sitemap(client, "https://example.com/sitemap.xml.gz", depth=0)
    )
    assert err is None
    assert {c.url for c in candidates} == {
        "https://example.com/article-1",
        "https://example.com/article-2",
    }


def test_gzipped_sitemap_by_magic_bytes():
    # Same body but URL doesn't end in .gz — detection should still work via magic bytes.
    body = gzip.compress(SITEMAP_XML)
    client = _fake_client(body)
    candidates, err = asyncio.run(
        _walk_sitemap(client, "https://example.com/sitemap.xml", depth=0)
    )
    assert err is None
    assert len(candidates) == 2


def test_uncompressed_sitemap_still_works():
    client = _fake_client(SITEMAP_XML)
    candidates, err = asyncio.run(
        _walk_sitemap(client, "https://example.com/sitemap.xml", depth=0)
    )
    assert err is None
    assert len(candidates) == 2


def test_http_error_propagates():
    client = _fake_client(b"", status=404)
    candidates, err = asyncio.run(
        _walk_sitemap(client, "https://example.com/sitemap.xml", depth=0)
    )
    assert candidates == []
    assert err == "http_error"
