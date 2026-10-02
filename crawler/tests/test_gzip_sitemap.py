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
    client = _fake_client(b"", status=500)
    candidates, err = asyncio.run(
        _walk_sitemap(client, "https://example.com/sitemap.xml", depth=0)
    )
    assert candidates == []
    assert err == "http_error"


def test_missing_sitemap_is_not_found():
    # A 404 is "not there", not a refusal; all-not-found → no_feed_found.
    client = _fake_client(b"", status=404)
    candidates, err = asyncio.run(
        _walk_sitemap(client, "https://example.com/sitemap.xml", depth=0)
    )
    assert candidates == []
    assert err == "not_found"


def test_sitemap_with_leading_newline_parses():
    # Some publishers (e.g. The Nation, Nigeria) ship a stray newline before
    # the XML declaration, which lxml otherwise rejects.
    client = _fake_client(b"\n" + SITEMAP_XML)
    candidates, err = asyncio.run(
        _walk_sitemap(client, "https://example.com/sitemap.xml", depth=0)
    )
    assert err is None
    assert len(candidates) == 2


def test_sitemap_with_utf8_bom_parses():
    client = _fake_client(b"\xef\xbb\xbf" + SITEMAP_XML)
    candidates, err = asyncio.run(
        _walk_sitemap(client, "https://example.com/sitemap.xml", depth=0)
    )
    assert err is None
    assert len(candidates) == 2


LASTMOD_SITEMAP_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://example.com/older</loc>
    <lastmod>2026-05-03T11:52:27+02:00</lastmod>
  </url>
  <url>
    <loc>https://example.com/newer</loc>
    <lastmod>2026-05-26T05:00:00+02:00</lastmod>
  </url>
  <url>
    <loc>https://example.com/no-date</loc>
  </url>
</urlset>
"""


def test_lastmod_used_as_publication_date_fallback():
    # When <news:publication_date> is missing, fall back to <lastmod> so the
    # downstream sort surfaces the most recent URLs (e.g. Heute.at).
    client = _fake_client(LASTMOD_SITEMAP_XML)
    candidates, err = asyncio.run(
        _walk_sitemap(client, "https://example.com/sitemap.xml", depth=0)
    )
    assert err is None
    by_url = {c.url: c.publication_date for c in candidates}
    assert by_url["https://example.com/older"] is not None
    assert by_url["https://example.com/newer"] is not None
    assert by_url["https://example.com/no-date"] is None
    assert by_url["https://example.com/newer"] > by_url["https://example.com/older"]


def _routing_client(bodies: dict[str, bytes]):
    """Mock client whose .get returns a different body per URL."""
    client = MagicMock()

    async def _get(url, *args, **kwargs):
        response = MagicMock()
        response.status_code = 200 if url in bodies else 404
        response.content = bodies.get(url, b"")
        return response

    client.get = AsyncMock(side_effect=_get)
    return client


def test_sitemapindex_recurses_freshest_child_by_lastmod():
    # Paginated indexes (Yoast post-sitemap{N}, Tengrinews …-news-{N}) list
    # children oldest->newest, so the freshest articles are in the LAST child.
    # With 16 children, document-order [:10] would never reach it; sorting by
    # <lastmod> desc must surface it. The fresh child is listed LAST and is the
    # only one with a recent lastmod.
    INDEX = "https://example.com/sitemap-index.xml"
    bodies = {}
    children_xml = []
    for i in range(15):  # stale children, listed first
        loc = f"https://example.com/old-{i}.xml"
        children_xml.append(
            f"<sitemap><loc>{loc}</loc><lastmod>2020-01-0{i % 9 + 1}T00:00:00+00:00</lastmod></sitemap>"
        )
        bodies[loc] = (
            b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + f'<url><loc>https://example.com/old-article-{i}</loc></url></urlset>'.encode()
        )
    fresh_loc = "https://example.com/news-43.xml"
    children_xml.append(
        f"<sitemap><loc>{fresh_loc}</loc><lastmod>2026-06-15T03:00:00+00:00</lastmod></sitemap>"
    )
    bodies[fresh_loc] = (
        b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b'<url><loc>https://example.com/fresh-article</loc></url></urlset>'
    )
    bodies[INDEX] = (
        '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(children_xml)
        + "</sitemapindex>"
    ).encode()

    client = _routing_client(bodies)
    candidates, err = asyncio.run(_walk_sitemap(client, INDEX, depth=0))
    assert err is None
    urls = {c.url for c in candidates}
    assert "https://example.com/fresh-article" in urls
