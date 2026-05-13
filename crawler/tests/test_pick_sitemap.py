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
