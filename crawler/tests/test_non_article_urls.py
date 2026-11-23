"""Discovery drops URLs that are evidently not news stories.

Cases come from the 2026-11-01 run, where sitemaps fed section fronts, tag
pages and utility pages into the sample (Hindustan Times, TimesLIVE, The Star,
Orda.kz, Rudaw, Quartz, Tengrinews), and from the real stories that a looser
rule would have dropped (Malawi Nation, La República, Guyana Times, which use
short WordPress slugs in their RSS feeds).

No network: requests go through ``httpx.MockTransport``.
"""
import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from pmd_crawler.discovery import _walk_sitemap, fetch_rss_articles, non_article_reason

NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
XML = {"content-type": "text/xml; charset=UTF-8"}


# --- the rule itself ---------------------------------------------------------

@pytest.mark.parametrize("url, reason", [
    # Homepages and language roots
    ("https://www.dawn.com/", "homepage"),
    ("https://paperjam.lu", "homepage"),
    ("https://rudaw.net/english", "section"),
    ("https://petra.gov.jo/ar", "section"),
    # Section fronts
    ("https://www.hindustantimes.com/entertainment/bollywood", "section"),
    ("https://www.hindustantimes.com/lifestyle", "section"),
    ("https://www.timeslive.co.za/news/world/", "section"),
    ("https://www.thestar.com.my/starplus", "section"),
    ("https://tengrinews.kz/world_news/", "section"),
    ("https://paperjam.lu/list/picturereport", "section"),
    ("https://www.rudaw.net/english/categories/culture", "section"),
    ("https://www.rudaw.net/kurmanci/categories/sport-cup", "section"),
    # Tag and author listings
    ("https://orda.kz/tag/avariya-na-gazoprovode/", "listing"),
    ("https://www.cas.sk/tag/ondrej-mestak", "listing"),
    ("https://tengrinews.kz/author/78/", "listing"),
    # Utility pages, at any depth
    ("https://rudaw.net/sorani/search", "static"),
    ("https://rudaw.net/sorani/more", "static"),
    ("https://qz.com/login", "static"),
    ("https://qz.com/emails", "static"),
    ("https://qz.com/about", "static"),
])
def test_non_articles_rejected_from_sitemaps(url, reason):
    assert non_article_reason(url, from_sitemap=True) == reason


@pytest.mark.parametrize("url", [
    "https://www.dawn.com/news/2041352/lpg-price-twice-the-official-rate-in-open-market",
    "https://www.timeslive.co.za/news/world/2026-11-23-bangladesh-vows-to-arrest-ex-pm",
    "https://www.rudaw.net/english/categories/syria/1080273",
    "https://qz.com/tjx-raises-annual-outlook-third-quarter-revenue-beat-111926",
    "https://qz.com/artemis-ii-crew-mission-moon-flyby-nasa",
    "https://japantoday.com/category/world/uk-prime-minister-signals-apology",
    "https://www.freemalaysiatoday.com/category/world/2026/11/01/bolivia-declares-emergency",
    "https://www.mirror.co.uk/tv/tv-news/sainsburys-christmas-ad-2026-bfg-37824894",
    "https://www.politico.com/video/2026/10/27/trump-gop-money-universe-2213216",
    "https://novinite.com/view_news.php?id=241361",
    "https://informante.web.na/?p=404227",
    "https://xs.uz/ru/news/-25405cce",
    "https://www.cnn.com/2026/11/23/economy/new-home-builds-construction-problems",
    # "search" inside a slug is not the /search page
    "https://example.com/news/search-for-survivors-continues",
    # "tag" as the last segment is a slug, not a listing prefix
    "https://example.com/news/2026/11/tag",
    # an old Rudaw article whose ID is only five digits
    "https://www.rudaw.net/arabic/categories/iran/52837",
])
def test_stories_kept_from_sitemaps(url):
    assert non_article_reason(url, from_sitemap=True) is None


@pytest.mark.parametrize("url", [
    "https://mwnation.com/sisters-backing/",
    "https://www.larepublica.net/festival-coreano/",
    "https://guyanatimesgy.com/hailing/",
])
def test_short_slugs_kept_from_rss(url):
    # Same shape as a section front, but RSS items are almost always stories.
    assert non_article_reason(url, from_sitemap=False) is None
    assert non_article_reason(url, from_sitemap=True) == "section"


@pytest.mark.parametrize("url, reason", [
    ("https://vietnamnews.vn/", "homepage"),
    ("https://orda.kz/tag/aviakompaniya/", "listing"),
    ("https://qz.com/login", "static"),
])
def test_unambiguous_non_articles_rejected_from_rss(url, reason):
    assert non_article_reason(url, from_sitemap=False) == reason


# --- wired into discovery ----------------------------------------------------

def _client(routes: dict[str, httpx.Response]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(str(request.url), httpx.Response(404))
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_sitemap_walk_skips_fresh_static_pages():
    # Rudaw's shape: utility pages carry the sitemap's generation time as
    # <lastmod>, so without the filter they sort above every article.
    url = "https://www.rudaw.net/sitemap.xml"
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="{NS}">
  <url><loc>https://www.rudaw.net/english</loc><lastmod>2026-11-23T09:45:39Z</lastmod></url>
  <url><loc>https://www.rudaw.net/sorani/search</loc><lastmod>2026-11-23T09:45:39Z</lastmod></url>
  <url><loc>https://www.rudaw.net/english/categories/weather</loc><lastmod>2026-11-23T09:45:39Z</lastmod></url>
  <url><loc>https://www.rudaw.net/english/categories/syria/1080273</loc><lastmod>2026-11-23T08:00:00Z</lastmod></url>
</urlset>""".encode()

    async def run():
        async with _client({url: httpx.Response(200, headers=XML, content=body)}) as c:
            return await _walk_sitemap(c, url, depth=0)

    candidates, err = asyncio.run(run())
    assert err is None
    assert [c.url for c in candidates] == ["https://www.rudaw.net/english/categories/syria/1080273"]


def test_rss_skips_homepage_item_but_keeps_short_slug():
    url = "https://example.com/feed/"
    now = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    body = f"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title>
  <item><title>Home</title><link>https://example.com/</link><pubDate>{now}</pubDate></item>
  <item><title>Hailing</title><link>https://example.com/hailing/</link><pubDate>{now}</pubDate></item>
</channel></rss>""".encode()

    async def run():
        async with _client({url: httpx.Response(200, headers=XML, content=body)}) as c:
            return await fetch_rss_articles(c, url, window_days=30, max_articles=20)

    articles, err = asyncio.run(run())
    assert err is None
    assert [a.url for a in articles] == ["https://example.com/hailing/"]
