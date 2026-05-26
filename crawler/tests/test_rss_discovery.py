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


# ─── RSS image extraction ──────────────────────────────────────────────────

from pmd_crawler.discovery import _images_from_rss_entry  # noqa: E402


def test_images_from_media_content():
    entry = {
        "media_content": [
            {"url": "https://cdn.example.com/lead.jpg", "type": "image/jpeg"},
        ],
    }
    assert _images_from_rss_entry(entry) == ["https://cdn.example.com/lead.jpg"]


def test_images_from_media_content_no_type_assumed_image():
    # De Standaard / Het Nieuwsblad's RSS gives a bare media:content with a
    # url and no type — feedparser surfaces it as just {"url": ...}.
    entry = {"media_content": [{"url": "https://cdn.example.com/lead.jpg"}]}
    assert _images_from_rss_entry(entry) == ["https://cdn.example.com/lead.jpg"]


def test_images_from_enclosure_image_type():
    entry = {
        "enclosures": [
            {"href": "https://cdn.example.com/lead.jpg", "type": "image/jpeg"},
        ],
    }
    assert _images_from_rss_entry(entry) == ["https://cdn.example.com/lead.jpg"]


def test_images_skip_non_image_enclosures():
    entry = {
        "enclosures": [
            {"href": "https://cdn.example.com/podcast.mp3", "type": "audio/mpeg"},
            {"href": "https://cdn.example.com/video.mp4", "type": "video/mp4"},
        ],
    }
    assert _images_from_rss_entry(entry) == []


def test_images_dedupe_across_sources():
    entry = {
        "media_content": [{"url": "https://cdn.example.com/lead.jpg"}],
        "enclosures": [
            {"href": "https://cdn.example.com/lead.jpg", "type": "image/jpeg"},
        ],
    }
    assert _images_from_rss_entry(entry) == ["https://cdn.example.com/lead.jpg"]


def test_images_from_media_thumbnail():
    entry = {"media_thumbnail": [{"url": "https://cdn.example.com/thumb.jpg"}]}
    assert _images_from_rss_entry(entry) == ["https://cdn.example.com/thumb.jpg"]


def test_images_empty_entry():
    assert _images_from_rss_entry({}) == []


def test_images_from_content_encoded_img_tag():
    # Neue.at-style: no Media RSS / enclosures; image lives inline in
    # <content:encoded>, which feedparser surfaces on entry.content.
    entry = {
        "content": [
            {"type": "text/html", "value": (
                "<p>Some intro text.</p>"
                "<img src=\"https://cdn.example.com/article-lead.jpg\" alt=\"x\">"
                "<p>More body.</p>"
            )},
        ],
    }
    assert _images_from_rss_entry(entry) == [
        "https://cdn.example.com/article-lead.jpg"
    ]


def test_images_from_summary_when_no_content():
    entry = {"summary": '<img src="https://cdn.example.com/hero.jpg">More text.'}
    assert _images_from_rss_entry(entry) == ["https://cdn.example.com/hero.jpg"]


def test_images_inline_skip_data_uri_and_svg():
    entry = {
        "content": [{"value": (
            '<img src="data:image/png;base64,iVBORw0...">'
            '<img src="https://cdn.example.com/spacer.svg">'
            '<img src="https://cdn.example.com/real.jpg">'
        )}],
    }
    assert _images_from_rss_entry(entry) == ["https://cdn.example.com/real.jpg"]


def test_media_content_wins_over_inline_img():
    # When both are present, the Media RSS image comes first.
    entry = {
        "media_content": [{"url": "https://cdn.example.com/media.jpg"}],
        "content": [{"value": '<img src="https://cdn.example.com/inline.jpg">'}],
    }
    assert _images_from_rss_entry(entry) == [
        "https://cdn.example.com/media.jpg",
        "https://cdn.example.com/inline.jpg",
    ]
