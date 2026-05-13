from datetime import UTC, datetime, timedelta

from pmd_crawler.discovery import _entry_datetime


def test_entry_datetime_prefers_published():
    entry = {
        "published_parsed": (2026, 5, 12, 10, 30, 0, 0, 0, 0),
        "updated_parsed": (2026, 1, 1, 0, 0, 0, 0, 0, 0),
    }
    dt = _entry_datetime(entry)
    assert dt == datetime(2026, 5, 12, 10, 30, tzinfo=UTC)


def test_entry_datetime_falls_back_to_updated():
    entry = {"updated_parsed": (2026, 5, 12, 10, 30, 0, 0, 0, 0)}
    assert _entry_datetime(entry) == datetime(2026, 5, 12, 10, 30, tzinfo=UTC)


def test_entry_datetime_no_date():
    assert _entry_datetime({}) is None


def test_entry_datetime_within_window():
    # Sanity that returned datetime is timezone-aware and comparable.
    entry = {"published_parsed": (2026, 5, 12, 10, 30, 0, 0, 0, 0)}
    dt = _entry_datetime(entry)
    assert dt is not None
    assert dt >= datetime.now(UTC) - timedelta(days=365)


def test_guess_rss_urls_covers_common_paths():
    from pmd_crawler.discovery import _guess_rss_urls
    urls = _guess_rss_urls("https://example.com/")
    assert "https://example.com/feed" in urls
    assert "https://example.com/rss.xml" in urls
    assert "https://example.com/atom.xml" in urls
