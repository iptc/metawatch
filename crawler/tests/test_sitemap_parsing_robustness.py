"""Sitemaps that are valid in practice but used to yield 0 candidates.

- Vísir serves plain text/xml at a ``.gz`` URL.
- people.com.cn's root index is ``<sitemapIndex>`` (camel case).
- Novo Jornal's Google News sitemap has an undeclared ``&ndash;`` entity.

No network: requests go through ``httpx.MockTransport``.
"""
import asyncio
import gzip

import httpx

from pmd_crawler.discovery import _walk_sitemap

NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
XML = {"content-type": "text/xml; charset=UTF-8"}
HTML = {"content-type": "text/html; charset=UTF-8"}

URLSET = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="{NS}">
  <url><loc>https://example.com/article-1</loc></url>
  <url><loc>https://example.com/article-2</loc></url>
</urlset>
""".encode()


def _walk(routes: dict[str, httpx.Response], url: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(str(request.url), httpx.Response(404))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await _walk_sitemap(client, url, depth=0)

    return asyncio.run(run())


# --- 1. .gz URL serving plain XML ------------------------------------------

def test_gz_url_serving_plain_xml_parses():
    url = "https://example.com/news/sitemap.gz"
    candidates, err = _walk({url: httpx.Response(200, headers=XML, content=URLSET)}, url)
    assert err is None
    assert {c.url for c in candidates} == {
        "https://example.com/article-1",
        "https://example.com/article-2",
    }


def test_gz_url_serving_real_gzip_still_decompresses():
    url = "https://example.com/news/sitemap.gz"
    body = gzip.compress(URLSET)
    candidates, err = _walk({url: httpx.Response(200, content=body)}, url)
    assert err is None
    assert len(candidates) == 2


def test_corrupt_gzip_body_is_parse_error():
    url = "https://example.com/sitemap.xml.gz"
    body = gzip.compress(URLSET)[:20]  # magic bytes present, stream truncated
    candidates, err = _walk({url: httpx.Response(200, content=body)}, url)
    assert candidates == []
    assert err == "parse_error"


# --- 2. case-insensitive tags ----------------------------------------------

def test_camelcase_sitemapindex_root_recurses():
    index = "https://example.com/sitemap_index.xml"
    child = "https://example.com/news_sitemap.xml"
    index_xml = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sitemapIndex xmlns="{NS}"><sitemap><loc>{child}</loc>'
        f"<lastmod>2026-11-19</lastmod></sitemap></sitemapIndex>"
    ).encode()
    candidates, err = _walk(
        {
            index: httpx.Response(200, headers=XML, content=index_xml),
            child: httpx.Response(200, headers=XML, content=URLSET),
        },
        index,
    )
    assert err is None
    assert len(candidates) == 2


def test_mixed_case_child_elements_parse():
    url = "https://example.com/sitemap.xml"
    body = (
        f'<?xml version="1.0"?><URLSet xmlns="{NS}">'
        f"<URL><Loc>https://example.com/a</Loc><LastMod>2026-11-01</LastMod></URL>"
        f"</URLSet>"
    ).encode()
    candidates, err = _walk({url: httpx.Response(200, headers=XML, content=body)}, url)
    assert err is None
    assert [c.url for c in candidates] == ["https://example.com/a"]
    assert candidates[0].publication_date is not None


# --- 3. malformed XML recovery ----------------------------------------------

NEWS_WITH_ENTITY = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="{NS}" xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
  <url>
    <loc>https://example.com/noticia-1</loc>
    <news:news>
      <news:publication_date>2026-11-18T10:00:00+01:00</news:publication_date>
      <news:title>Luanda &ndash; economia</news:title>
    </news:news>
  </url>
  <url><loc>https://example.com/noticia-2</loc></url>
</urlset>
""".encode()


def test_undeclared_entity_recovers():
    url = "https://example.com/google_news.ashx"
    candidates, err = _walk(
        {url: httpx.Response(200, headers=XML, content=NEWS_WITH_ENTITY)}, url
    )
    assert err is None
    assert {c.url for c in candidates} == {
        "https://example.com/noticia-1",
        "https://example.com/noticia-2",
    }


def test_waf_challenge_still_reports_waf():
    url = "https://example.com/sitemap.xml"
    page = b"<html><head><title>Just a moment...</title></head><body><p>x</body></html>"
    candidates, err = _walk(
        {url: httpx.Response(403, headers={**HTML, "cf-ray": "8a1b"}, content=page)}, url
    )
    assert candidates == []
    assert err == "waf:cloudflare"


def test_waf_challenge_with_200_still_reports_waf():
    url = "https://example.com/sitemap.xml"
    page = (b"<!DOCTYPE html><html><head><title>Just a moment...</title>"
            b"<script>window._cf_chl_opt={}</script><body><br></body></html>")
    candidates, err = _walk(
        {url: httpx.Response(200, headers={**HTML, "cf-ray": "8a1b"}, content=page)}, url
    )
    assert candidates == []
    assert err == "waf:cloudflare"


def test_soft_404_html_still_not_found():
    url = "https://example.com/sitemap.xml"
    page = b"<!DOCTYPE html><html><body><p>Not found.<br></body></html>"
    candidates, err = _walk({url: httpx.Response(200, headers=HTML, content=page)}, url)
    assert candidates == []
    assert err == "not_found"


def test_unrecoverable_garbage_served_as_xml_is_parse_error():
    url = "https://example.com/sitemap.xml"
    body = b"<?xml version='1.0'?>\n<<< not a sitemap >>>"
    candidates, err = _walk({url: httpx.Response(200, headers=XML, content=body)}, url)
    assert candidates == []
    assert err == "parse_error"


def test_broken_html_served_as_xml_does_not_recover_into_sitemap():
    url = "https://example.com/sitemap.xml"
    body = b"<html><body><p>oops &nbsp;<br></body></html>"
    candidates, err = _walk({url: httpx.Response(200, headers=XML, content=body)}, url)
    assert candidates == []
    assert err == "parse_error"
