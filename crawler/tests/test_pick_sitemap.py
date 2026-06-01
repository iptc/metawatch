from pmd_crawler.config import DiscoveryConfig, SampleConfig, Site
from pmd_crawler.discovery import pick_sitemap


def _site(**discovery_kwargs) -> Site:
    return Site(
        id="x", name="X", url="https://x.example/", country="XX",
        homepage="https://x.example/",
        discovery=DiscoveryConfig(**discovery_kwargs),
        sample=SampleConfig(),
    )


def test_config_picture_wins_when_set():
    s = _site(picture_sitemap_url="https://x.example/picture.xml")
    url, strategy = pick_sitemap(["https://x.example/news-sitemap.xml"], s)
    assert (url, strategy) == ("https://x.example/picture.xml", "config:picture_sitemap")


def test_config_sitemap_beats_robots():
    s = _site(sitemap_urls=["https://x.example/configured.xml"])
    url, strategy = pick_sitemap(["https://x.example/news-sitemap.xml"], s)
    assert (url, strategy) == ("https://x.example/configured.xml", "config:sitemap")


def test_news_sitemap_preferred_over_image():
    # Regression: previously "picture" / "image" sitemaps were picked first,
    # causing 70+ "no_articles_found" cases — image sitemaps have no <url>
    # entries so _parse_urlset returned zero candidates.
    robots = [
        "https://x.example/sitemap.xml",
        "https://x.example/sitemap-image.xml",
        "https://x.example/sitemap-news.xml",
    ]
    url, strategy = pick_sitemap(robots, _site())
    assert (url, strategy) == ("https://x.example/sitemap-news.xml", "robots:news_sitemap")


def test_first_non_picture_chosen_when_no_news_sitemap():
    robots = [
        "https://x.example/sitemap-image.xml",
        "https://x.example/sitemap.xml",
        "https://x.example/sitemap-video.xml",
    ]
    url, strategy = pick_sitemap(robots, _site())
    assert (url, strategy) == ("https://x.example/sitemap.xml", "robots:first_sitemap")


def test_picture_sitemap_used_only_as_last_resort():
    robots = ["https://x.example/sitemap-image.xml"]
    url, strategy = pick_sitemap(robots, _site())
    assert (url, strategy) == (
        "https://x.example/sitemap-image.xml",
        "robots:picture_sitemap_fallback",
    )


def test_no_sitemaps_at_all():
    assert pick_sitemap([], _site()) == (None, "fallback_needed")


# ─── _looks_like_xml_sitemap (SPA catch-all 200 decoy detection) ────────────


from pmd_crawler.discovery import _looks_like_xml_sitemap as _xml


def test_xml_prologue_accepted():
    assert _xml('<?xml version="1.0"?>\n<urlset></urlset>', "application/xml")


def test_urlset_root_without_prologue_accepted():
    # Some sitemaps skip the XML declaration. Accept on root-element shape.
    assert _xml('<urlset xmlns="...">...</urlset>', "text/xml")


def test_sitemap_index_accepted():
    assert _xml('<sitemapindex>...</sitemapindex>', "text/xml")


def test_rss_feed_accepted():
    # Same path is reused for feed probes — RSS / Atom shapes too.
    assert _xml('<rss version="2.0">', "application/rss+xml")
    assert _xml('<feed xmlns="http://www.w3.org/2005/Atom">', "application/atom+xml")


def test_spa_html_decoy_rejected_even_when_ctype_says_xml():
    # The exact bug class this guard exists for: SPA returns 200 + HTML,
    # sometimes with a misleading content-type. The body sniff wins.
    assert not _xml('<!DOCTYPE html><html>...</html>', "text/xml")


def test_html_with_html_ctype_rejected():
    assert not _xml('<!doctype html><html><body>404</body></html>', "text/html; charset=utf-8")


def test_xml_ctype_alone_is_enough_for_non_html_body():
    # A whitespace-only body with an XML ctype is uncommon but not malicious;
    # we'd rather try the downstream parse than discard outright.
    assert _xml('   \n  ', "application/xml")


def test_empty_body_with_no_xml_ctype_rejected():
    assert not _xml('', "")
