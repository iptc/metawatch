import json

from pmd_crawler.articles import _images_from_jsonld


def test_jsonld_type_as_list_does_not_raise():
    # Regression: some publishers emit @type as a list, e.g. ["Article","NewsArticle"].
    # The old code did `t in {"NewsArticle", ...}` which raises unhashable type: 'list'.
    raw = json.dumps({"@type": ["Article", "NewsArticle"], "image": "https://x/y.jpg"})
    assert _images_from_jsonld(raw) == ["https://x/y.jpg"]


def test_jsonld_type_as_string_still_works():
    raw = json.dumps({"@type": "NewsArticle", "image": "https://x/y.jpg"})
    assert _images_from_jsonld(raw) == ["https://x/y.jpg"]


def test_jsonld_unknown_type_returns_no_images():
    raw = json.dumps({"@type": "WebPage", "image": "https://x/y.jpg"})
    assert _images_from_jsonld(raw) == []


def test_jsonld_image_as_object():
    raw = json.dumps({"@type": "NewsArticle", "image": {"url": "https://x/y.jpg"}})
    assert _images_from_jsonld(raw) == ["https://x/y.jpg"]
