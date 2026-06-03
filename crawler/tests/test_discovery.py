from datetime import UTC, datetime

from selectolax.parser import HTMLParser

from pmd_crawler.discovery import _anchor_feed_candidates, _parse_iso_date


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


def test_anchor_feed_candidate_matches_text():
    # The Malta Independent case: feed linked as a plain body anchor whose
    # visible text is "RSS" rather than a <link rel="alternate"> in the head.
    html = '<html><body><a href="/newsfeed">RSS</a></body></html>'
    out = _anchor_feed_candidates(HTMLParser(html), "https://example.com/")
    assert out == ["https://example.com/newsfeed"]


def test_anchor_feed_candidate_matches_href_path():
    # Anchor text gives no hint, but the href path contains /feed/.
    html = '<html><body><a href="/feed/">Subscribe</a></body></html>'
    out = _anchor_feed_candidates(HTMLParser(html), "https://example.com/")
    assert out == ["https://example.com/feed/"]


def test_anchor_feed_candidate_skips_social_share():
    # A social-share link to Twitter must not be mistaken for a feed even
    # though its text/href can contain feed-ish substrings.
    html = (
        '<html><body>'
        '<a href="https://twitter.com/intent/tweet?url=x">Share to feed</a>'
        '</body></html>'
    )
    out = _anchor_feed_candidates(HTMLParser(html), "https://example.com/")
    assert out == []
