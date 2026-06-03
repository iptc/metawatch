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
