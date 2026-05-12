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
