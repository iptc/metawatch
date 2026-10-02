from datetime import UTC, datetime

from pmd_crawler.discovery import _parse_iso_date


def test_iso_date_with_z_suffix():
    dt = _parse_iso_date("2026-05-12T10:30:00Z")
    assert dt == datetime(2026, 5, 12, 10, 30, tzinfo=UTC)


def test_iso_date_with_offset():
    dt = _parse_iso_date("2026-05-12T10:30:00+02:00")
    assert dt is not None and dt.tzinfo is not None


def test_iso_date_naive_is_promoted_to_utc():
    # Regression: some sitemaps publish naive dates (no Z, no offset).
    # The old code returned a naive datetime that couldn't be compared to
    # the UTC cutoff, raising "can't compare offset-naive and offset-aware".
    dt = _parse_iso_date("2026-05-12T10:30:00")
    assert dt == datetime(2026, 5, 12, 10, 30, tzinfo=UTC)


def test_iso_date_invalid_returns_none():
    assert _parse_iso_date("not a date") is None


def test_clean_title_strips_cdata_wrapper():
    from pmd_crawler.discovery import _clean_title
    assert _clean_title("<![CDATA[ Dois mortos nas praias ]]>") == "Dois mortos nas praias"


def test_clean_title_preserves_plain_and_inner_quotes():
    from pmd_crawler.discovery import _clean_title
    assert _clean_title("Plain headline") == "Plain headline"
    assert _clean_title('<![CDATA[ Marisa Liz: "uma estranha" ]]>') == 'Marisa Liz: "uma estranha"'


def test_clean_title_empty_and_none():
    from pmd_crawler.discovery import _clean_title
    assert _clean_title(None) is None
    assert _clean_title("   ") is None
    assert _clean_title("<![CDATA[]]>") is None


def test_is_probable_article_url_rejects_non_article_extensions():
    from pmd_crawler.discovery import _is_probable_article_url as ok
    # Secondary resources that have shown up inside <url><loc> in real sitemaps:
    assert ok("https://www.vcg.com/sitemap-20260602-1.xml") is False   # Visual China
    assert ok("https://pamediagroup.com/locations.kml") is False        # PA Media
    assert ok("https://x/feed.xml.gz") is False
    assert ok("https://x/photo.JPG") is False                           # case-insensitive
    assert ok("https://x/report.pdf") is False


def test_is_probable_article_url_keeps_real_articles():
    from pmd_crawler.discovery import _is_probable_article_url as ok
    assert ok("https://wyborcza.pl/7,75399,32829442,trump.html") is True
    assert ok("https://www.gazeta.ru/army/news/2026/06/02/28594699.shtml") is True
    assert ok("http://www.baltictimes.com/some_story/") is True          # trailing slash
    assert ok("http://www.televideo.rai.it/pub/view.jsp?id=172&p=101") is True  # query string
    assert ok("https://example.com/news/some-story") is True


# ─── Robots can_fetch (honored on every path, incl. configured RSS) ──────────

def test_robotsdecision_can_fetch_blocks_when_disallowed():
    from pmd_crawler.discovery import RobotsDecision
    rd = RobotsDecision(True, "User-agent: *\nDisallow: /\n", False, [], None)
    assert rd.can_fetch("https://x.com/") is False
    assert rd.can_fetch("https://x.com/world/story") is False


def test_robotsdecision_can_fetch_fails_open_when_not_fetched():
    # WAF dropped robots.txt -> we must still crawl (don't penalise hidden robots).
    from pmd_crawler.discovery import RobotsDecision
    rd = RobotsDecision(False, "", True, [], None)
    assert rd.can_fetch("https://x.com/anything") is True


def test_robotsdecision_can_fetch_honors_path_level_disallow():
    # Root allowed but a specific section disallowed (the APA /newsfeed case):
    # per-URL filtering must drop only the disallowed section.
    from pmd_crawler.discovery import RobotsDecision
    rd = RobotsDecision(True, "User-agent: *\nDisallow: /newsfeed/\n", True, [], None)
    assert rd.can_fetch("https://apa.at/") is True
    assert rd.can_fetch("https://apa.at/newsfeed/x") is False
    assert rd.can_fetch("https://apa.at/other/x") is True


# ─── tracking-param stripping (canonical URLs + correct robots handling) ─────

def test_strip_tracking_params_removes_ref_and_utm():
    from pmd_crawler.discovery import strip_tracking_params
    u = "https://www.smh.com.au/national/story-p605o0.html?ref=rss&utm_medium=rss&utm_source=rss_feed"
    assert strip_tracking_params(u) == "https://www.smh.com.au/national/story-p605o0.html"


def test_strip_tracking_params_keeps_meaningful_query():
    from pmd_crawler.discovery import strip_tracking_params
    # ?id=123 identifies the article — must be preserved.
    u = "https://example.com/view?id=123&utm_source=newsletter"
    assert strip_tracking_params(u) == "https://example.com/view?id=123"


def test_strip_tracking_params_noop_without_query():
    from pmd_crawler.discovery import strip_tracking_params
    assert strip_tracking_params("https://example.com/a/b") == "https://example.com/a/b"


# --- WAF detection (blocked_by_waf status) ---
import httpx
from pmd_crawler.discovery import _detect_waf, status_for_empty_discovery


def _resp(status, headers=None, body=b""):
    return httpx.Response(status_code=status, headers=headers or {}, content=body)


def test_detect_waf_cloudflare_just_a_moment_body():
    r = _resp(403, {"content-type": "text/html"}, b"<title>Just a moment...</title>")
    assert _detect_waf(r) == "cloudflare"


def test_detect_waf_cloudflare_header_on_refusal():
    # cf-ray header on a 403 is a block even without a body marker.
    r = _resp(403, {"cf-ray": "8a1b2c", "content-type": "text/html"}, b"<html>blocked</html>")
    assert _detect_waf(r) == "cloudflare"


def test_detect_waf_akamai_header_refusal():
    r = _resp(403, {"server": "AkamaiGHost", "content-type": "text/html"},
              b"<h1>Access Denied</h1> Reference&#32;#18.abcd")
    assert _detect_waf(r) == "akamai"


def test_detect_waf_datadome_header():
    r = _resp(403, {"x-datadome": "protected", "content-type": "text/html"}, b"datadome")
    assert _detect_waf(r) == "datadome"


def test_detect_waf_soft_404_html_is_not_waf():
    # A catch-all homepage / soft-404 returns 200 text/html for an unknown feed
    # URL. No vendor header, no challenge marker -> NOT a WAF (user's point).
    r = _resp(200, {"content-type": "text/html"},
              b"<!DOCTYPE html><html><head><title>Home</title></head><body>Welcome</body></html>")
    assert _detect_waf(r) is None


def test_detect_waf_imperva_404_is_not_a_block():
    # pap.pl: guessed feed paths 404, and Imperva injects its resource script
    # into those 404 pages. Vendor header + vendor marker, but "not found" is
    # an answer, not a refusal -> NOT a WAF block.
    body = (b'<html><head><title>Strona nieznaleziona</title>'
            b'<script src="/_Incapsula_Resource?SWJIYLWA=5074"></script></head></html>')
    for status in (404, 410):
        r = _resp(status, {"x-cdn": "Imperva", "x-iinfo": "45-1", "content-type": "text/html"}, body)
        assert _detect_waf(r) is None
    # The same page as a 403 is still a block.
    r = _resp(403, {"x-cdn": "Imperva", "x-iinfo": "45-1", "content-type": "text/html"}, body)
    assert _detect_waf(r) == "imperva"


def test_detect_waf_generic_403_without_fingerprint_is_not_waf():
    # A bare 403 with no vendor header and no challenge body stays http_error.
    r = _resp(403, {"content-type": "text/plain"}, b"Forbidden")
    assert _detect_waf(r) is None


def test_detect_waf_cloudflare_fronted_200_feed_is_not_block():
    # A real RSS feed served *through* Cloudflare (cf-ray present, 200 OK) must
    # not be flagged — only refusals/challenges are.
    r = _resp(200, {"cf-ray": "8a1b2c", "content-type": "application/rss+xml"},
              b"<?xml version='1.0'?><rss><channel><title>News</title></channel></rss>")
    assert _detect_waf(r) is None


def test_status_for_empty_discovery_maps_waf():
    # Producers emit the vendor-qualified form "waf:<vendor>" (discovery.py
    # builds it in four places), never a bare "waf_blocked" — main.py parses
    # the vendor back out of this same string.
    assert status_for_empty_discovery("config:rss", "waf:cloudflare") == "blocked_by_waf"
    assert status_for_empty_discovery("config:sitemap", "waf:akamai") == "blocked_by_waf"
    # Non-WAF kinds are unchanged.
    assert status_for_empty_discovery("config:rss", "http_error") == "discovery_blocked"
    assert status_for_empty_discovery("config:sitemap", "parse_error") == "discovery_parse_error"


# ─── Candidate de-duplication ────────────────────────────────────────────────


def _cand(url, *, date=None, title=None, language=None, keywords=None, images=None):
    from pmd_crawler.discovery import ArticleCandidate
    return ArticleCandidate(
        url=url, publication_date=date, title=title, language=language,
        keywords=keywords or [], image_urls_from_sitemap=images or [],
    )


def test_dedupe_collapses_repeated_urls():
    from pmd_crawler.discovery import dedupe_candidates
    out = dedupe_candidates([_cand("https://x/a"), _cand("https://x/b"), _cand("https://x/a")])
    assert [c.url for c in out] == ["https://x/a", "https://x/b"]


def test_dedupe_news_copy_supplies_title_date_and_language():
    # The CRHoy case: the same article in sitemap-latest.xml (lastmod only,
    # listed first) and sitemap-news.xml (full <news:news> block). The news
    # copy's publication_date is the real one; lastmod is just the last edit.
    from pmd_crawler.discovery import dedupe_candidates
    lastmod = datetime(2026, 7, 31, 18, 56, tzinfo=UTC)
    news_date = datetime(2026, 7, 31, 18, 7, tzinfo=UTC)
    out = dedupe_candidates([
        _cand("https://crhoy.com/a", date=lastmod),
        _cand("https://crhoy.com/a", date=news_date, title="La FIFA retira el plan",
              language="es", keywords=["fifa"]),
    ])
    assert len(out) == 1
    assert out[0].title == "La FIFA retira el plan"
    assert out[0].language == "es"
    assert out[0].keywords == ["fifa"]
    assert out[0].publication_date == news_date


def test_dedupe_keeps_first_title_when_both_copies_have_one():
    from pmd_crawler.discovery import dedupe_candidates
    first = datetime(2026, 7, 31, 10, 0, tzinfo=UTC)
    out = dedupe_candidates([
        _cand("https://x/a", date=first, title="First"),
        _cand("https://x/a", date=datetime(2026, 7, 30, 10, 0, tzinfo=UTC), title="Second"),
    ])
    assert len(out) == 1
    assert out[0].title == "First"
    assert out[0].publication_date == first


def test_dedupe_fills_gaps_from_later_copy():
    from pmd_crawler.discovery import dedupe_candidates
    date = datetime(2026, 7, 31, 10, 0, tzinfo=UTC)
    out = dedupe_candidates([
        _cand("https://x/a", title="Story"),
        _cand("https://x/a", date=date, images=["https://cdn/x.jpg"]),
    ])
    assert out[0].publication_date == date
    assert out[0].image_urls_from_sitemap == ["https://cdn/x.jpg"]


# ─── Unbranded 200-status JS cookie challenges ───────────────────────────────

# Trimmed from the real gazeta.ru response: HTTP 200, ~4 KB, title "Document",
# a script that plants a cookie and calls location.replace, and nothing else.
_GAZETA_CHALLENGE = (
    "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"UTF-8\">"
    "<title>Document</title><script>(function(){var d=new Date().getTime();"
    "function uuid(){return 'xxxx'.replace(/x/g,function(){return 0;});}"
    "document.cookie='__bot='+uuid()+'; path=/';"
    "location.replace(window.location.pathname);})();</script></head><body></body></html>"
)


def test_js_challenge_detected_on_real_shape():
    from pmd_crawler.discovery import looks_like_js_challenge
    assert looks_like_js_challenge(_GAZETA_CHALLENGE) is True


def test_js_challenge_not_flagged_without_navigation():
    # Sets a cookie but never redirects — that's analytics, not a wall.
    from pmd_crawler.discovery import looks_like_js_challenge
    html = "<html><head><script>document.cookie='a=1';</script></head><body></body></html>"
    assert looks_like_js_challenge(html) is False


def test_js_challenge_not_flagged_when_article_furniture_present():
    # The safety net: a short page that sets a cookie and redirects but still
    # carries a real image is a document, not a challenge.
    from pmd_crawler.discovery import looks_like_js_challenge
    html = (
        "<html><head><script>document.cookie='a=1';location.replace('/x');</script>"
        "</head><body><img src=\"/lead.jpg\"></body></html>"
    )
    assert looks_like_js_challenge(html) is False


def test_js_challenge_not_flagged_on_a_full_size_page():
    # Size ceiling: a real article is far larger than any interstitial, even if
    # it happens to contain both markers.
    from pmd_crawler.discovery import looks_like_js_challenge
    html = (
        "<html><head><script>document.cookie='a=1';location.reload();</script></head>"
        "<body>" + ("x" * 13000) + "</body></html>"
    )
    assert looks_like_js_challenge(html) is False


def test_detect_waf_reports_js_challenge_vendor():
    r = _resp(200, {"content-type": "text/html"}, _GAZETA_CHALLENGE.encode())
    assert _detect_waf(r) == "js-challenge"


def test_status_for_empty_discovery_maps_js_challenge():
    assert status_for_empty_discovery("config:rss", "waf:js-challenge") == "blocked_by_waf"
