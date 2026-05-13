"""Robots.txt and sitemap-based article discovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
from lxml import etree
from protego import Protego
from selectolax.parser import HTMLParser

from . import USER_AGENT
from .config import Site

SITEMAP_NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
    "image": "http://www.google.com/schemas/sitemap-image/1.1",
}


@dataclass
class RobotsDecision:
    fetched: bool
    text: str
    allowed_at_root: bool
    sitemap_urls: list[str]
    crawl_delay: float | None


@dataclass
class ArticleCandidate:
    url: str
    publication_date: datetime | None
    title: str | None
    language: str | None
    keywords: list[str]
    image_urls_from_sitemap: list[str]


async def fetch_robots(client: httpx.AsyncClient, site: Site) -> RobotsDecision:
    robots_url = urljoin(site.url, "/robots.txt")
    try:
        r = await client.get(robots_url, timeout=15.0, follow_redirects=True)
        if r.status_code >= 400:
            return RobotsDecision(False, "", True, [], None)
        text = r.text
    except Exception:
        return RobotsDecision(False, "", True, [], None)

    parser = Protego.parse(text)
    allowed = parser.can_fetch(site.url, USER_AGENT)
    delay = parser.crawl_delay(USER_AGENT)
    sitemaps = list(parser.sitemaps)
    return RobotsDecision(True, text, allowed, sitemaps, float(delay) if delay else None)


def pick_sitemap(robots_sitemaps: list[str], site: Site) -> tuple[str | None, str]:
    """Return (chosen URL, strategy label).

    Order: explicit config picture > config sitemap > robots `news` > robots first
    non-picture > robots first > guesses. Picture/image sitemaps from robots are
    avoided unless nothing else is on offer — many sites publish image-only
    sitemaps with no <url> wrappers, so they yield zero article candidates.
    """
    if site.discovery.picture_sitemap_url:
        return site.discovery.picture_sitemap_url, "config:picture_sitemap"
    if site.discovery.sitemap_urls:
        return site.discovery.sitemap_urls[0], "config:sitemap"

    for u in robots_sitemaps:
        if "news" in urlparse(u).path.lower():
            return u, "robots:news_sitemap"
    for u in robots_sitemaps:
        if not _looks_like_picture_sitemap(u):
            return u, "robots:first_sitemap"
    if robots_sitemaps:
        return robots_sitemaps[0], "robots:picture_sitemap_fallback"

    return None, "fallback_needed"


def _looks_like_picture_sitemap(url: str) -> bool:
    path = urlparse(url).path.lower()
    return "picture" in path or "image" in path or "/video" in path


def _guess_sitemap_urls(site_url: str) -> list[str]:
    base = site_url.rstrip("/")
    return [
        f"{base}/sitemap-news.xml",
        f"{base}/news-sitemap.xml",
        f"{base}/sitemap_news.xml",
        f"{base}/news-sitemap-content.xml",
        f"{base}/sitemap.xml",
        f"{base}/sitemap_index.xml",
        f"{base}/sitemap-index.xml",
    ]


async def _try_guessed_sitemaps(client: httpx.AsyncClient, site: Site) -> tuple[str | None, str]:
    for url in _guess_sitemap_urls(site.url):
        try:
            r = await client.head(url, timeout=10.0, follow_redirects=True)
            if r.status_code < 400:
                return url, "guess:sitemap"
        except Exception:
            continue
    return None, "no_sitemap"


async def fetch_sitemap_articles(
    client: httpx.AsyncClient,
    site: Site,
    sitemap_url: str,
    window_days: int,
    max_articles: int,
) -> list[ArticleCandidate]:
    candidates = await _walk_sitemap(client, sitemap_url, depth=0)
    if not candidates:
        return []

    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    filtered = [
        c for c in candidates
        if c.publication_date is None or c.publication_date >= cutoff
    ]
    filtered.sort(
        key=lambda c: c.publication_date or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return filtered[:max_articles]


async def _walk_sitemap(
    client: httpx.AsyncClient,
    url: str,
    depth: int,
    seen: set[str] | None = None,
) -> list[ArticleCandidate]:
    if seen is None:
        seen = set()
    if url in seen or depth > 2:
        return []
    seen.add(url)

    try:
        r = await client.get(url, timeout=30.0, follow_redirects=True)
        r.raise_for_status()
    except Exception:
        return []

    try:
        root = etree.fromstring(r.content)
    except etree.XMLSyntaxError:
        return []

    tag = etree.QName(root.tag).localname

    if tag == "sitemapindex":
        out: list[ArticleCandidate] = []
        children = root.findall("sm:sitemap/sm:loc", SITEMAP_NS)
        for child in children[:10]:
            if child.text:
                out.extend(await _walk_sitemap(client, child.text.strip(), depth + 1, seen))
        return out

    if tag == "urlset":
        return _parse_urlset(root)

    return []


def _parse_urlset(root: etree._Element) -> list[ArticleCandidate]:
    out: list[ArticleCandidate] = []
    for url_el in root.findall("sm:url", SITEMAP_NS):
        loc_el = url_el.find("sm:loc", SITEMAP_NS)
        if loc_el is None or not loc_el.text:
            continue
        url = loc_el.text.strip()
        news_el = url_el.find("news:news", SITEMAP_NS)
        pub_date = None
        title = None
        language = None
        keywords: list[str] = []
        if news_el is not None:
            pub_el = news_el.find("news:publication_date", SITEMAP_NS)
            if pub_el is not None and pub_el.text:
                pub_date = _parse_iso_date(pub_el.text.strip())
            t_el = news_el.find("news:title", SITEMAP_NS)
            if t_el is not None and t_el.text:
                title = t_el.text.strip()
            lang_el = news_el.find("news:publication/news:language", SITEMAP_NS)
            if lang_el is not None and lang_el.text:
                language = lang_el.text.strip()
            kw_el = news_el.find("news:keywords", SITEMAP_NS)
            if kw_el is not None and kw_el.text:
                keywords = [k.strip() for k in kw_el.text.split(",") if k.strip()]

        image_urls: list[str] = []
        for img_el in url_el.findall("image:image/image:loc", SITEMAP_NS):
            if img_el.text:
                image_urls.append(img_el.text.strip())

        out.append(
            ArticleCandidate(
                url=url,
                publication_date=pub_date,
                title=title,
                language=language,
                keywords=keywords,
                image_urls_from_sitemap=image_urls,
            )
        )
    return out


def _parse_iso_date(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


async def fetch_rss_articles(
    client: httpx.AsyncClient,
    feed_url: str,
    window_days: int,
    max_articles: int,
) -> list[ArticleCandidate]:
    try:
        r = await client.get(feed_url, timeout=20.0, follow_redirects=True)
        if r.status_code >= 400 or not r.content:
            return []
    except Exception:
        return []
    parsed = feedparser.parse(r.content)
    if parsed.bozo and not parsed.entries:
        return []

    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    out: list[ArticleCandidate] = []
    for entry in parsed.entries:
        link = entry.get("link")
        if not link:
            continue
        pub = _entry_datetime(entry)
        if pub is not None and pub < cutoff:
            continue
        out.append(
            ArticleCandidate(
                url=link,
                publication_date=pub,
                title=entry.get("title"),
                language=None,
                keywords=[],
                image_urls_from_sitemap=[],
            )
        )
    out.sort(
        key=lambda c: c.publication_date or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return out[:max_articles]


def _entry_datetime(entry) -> datetime | None:
    """Extract a UTC datetime from a feedparser entry, trying published then updated."""
    for key in ("published_parsed", "updated_parsed"):
        struct = entry.get(key)
        if struct:
            try:
                return datetime(*struct[:6], tzinfo=UTC)
            except (TypeError, ValueError):
                continue
    return None


async def discover_rss_links_from_homepage(
    client: httpx.AsyncClient, homepage: str
) -> list[str]:
    """Return RSS/Atom URLs advertised in <link rel="alternate"> on the homepage."""
    try:
        r = await client.get(homepage, timeout=15.0, follow_redirects=True)
        if r.status_code >= 400 or not r.text:
            return []
    except Exception:
        return []
    tree = HTMLParser(r.text)
    seen: set[str] = set()
    out: list[str] = []
    for node in tree.css('link[rel="alternate"]'):
        type_attr = (node.attributes.get("type") or "").lower()
        href = node.attributes.get("href")
        if not href or ("rss" not in type_attr and "atom" not in type_attr):
            continue
        absolute = urljoin(str(r.url), href)
        if absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


async def discover(
    client: httpx.AsyncClient, site: Site
) -> tuple[RobotsDecision, str | None, str, list[ArticleCandidate]]:
    """Run the full discovery pipeline. Returns (robots, source_url, strategy, articles).

    Source priority — first to yield >=1 article wins:
      1. config:rss          — RSS URL(s) configured in the site YAML
      2. html:rss_link       — RSS URL(s) advertised in homepage <link rel="alternate">
      3. config:sitemap, etc — sitemap chain (config/robots/guessed)
    """
    robots = await fetch_robots(client, site)
    if not robots.allowed_at_root:
        return robots, None, "robots_disallow", []

    window_days = site.sample.window_days
    max_articles = site.sample.max_articles

    for url in site.discovery.rss_urls:
        articles = await fetch_rss_articles(client, url, window_days, max_articles)
        if articles:
            return robots, url, "config:rss", articles

    for url in await discover_rss_links_from_homepage(client, site.homepage or site.url):
        articles = await fetch_rss_articles(client, url, window_days, max_articles)
        if articles:
            return robots, url, "html:rss_link", articles

    sitemap_url, strategy = pick_sitemap(robots.sitemap_urls, site)
    if not sitemap_url:
        sitemap_url, strategy = await _try_guessed_sitemaps(client, site)
    if not sitemap_url:
        return robots, None, strategy, []

    articles = await fetch_sitemap_articles(
        client, site, sitemap_url, window_days, max_articles
    )
    return robots, sitemap_url, strategy, articles
