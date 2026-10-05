"""Robots.txt and sitemap-based article discovery."""

from __future__ import annotations

import asyncio
import gzip
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

import feedparser
import httpx
from lxml import etree
from protego import Protego
from rich.console import Console
from selectolax.lexbor import LexborHTMLParser as HTMLParser  # see articles.py

from . import USER_AGENT
from .config import Site

_console = Console(stderr=True)

# Single retry on transient feed-fetch errors. Three seconds is long enough
# that a momentary Fastly/Cloudflare rate-limit, origin hiccup, or DNS blip
# usually clears, and short enough not to dominate the per-site budget.
_RSS_RETRY_BACKOFF_S = 3.0

SITEMAP_NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
    "image": "http://www.google.com/schemas/sitemap-image/1.1",
}

# WAF / bot-management challenge fingerprints. We flag ``waf_blocked`` only on a
# POSITIVE signature: a vendor header paired with a refusal status, or a body
# marker specific to a challenge interstitial. We deliberately do NOT treat
# "got HTML when we expected XML" as a signal — a soft-404 or a catch-all
# homepage redirect returns HTML for an unknown feed URL too, and that isn't a
# block. Body markers are lowercased substrings unique enough that ordinary
# article content won't contain them.
_WAF_BODY_MARKERS: dict[str, tuple[str, ...]] = {
    "cloudflare": (
        "just a moment", "attention required! | cloudflare", "cf-browser-verification",
        "challenge-platform", "cf_chl_opt", "checking if the site connection is secure",
    ),
    "datadome": ("datadome", "captcha-delivery.com"),
    "imperva": ("_incapsula_resource", "incapsula incident id", "powered by incapsula"),
    "sucuri": ("sucuri website firewall", "access denied - sucuri website firewall"),
    # "access denied" is too generic to stand alone; only trusted with the header.
    "akamai": ("access denied", "reference&#32;#"),
}
_WAF_REFUSAL_CODES = frozenset({401, 403, 429, 503})


#: Upper bound on a JS-challenge shell. Real articles run to tens of KB; the
#: interstitials seen in the wild are 2–6 KB of script and nothing else.
_JS_CHALLENGE_MAX_BYTES = 12000

#: Ways a challenge page forces the reload once it has planted its cookie.
_JS_CHALLENGE_NAV = (
    "location.replace", "location.reload", "location.href", "location.assign",
)


def looks_like_js_challenge(html: str) -> bool:
    """True when an HTTP 200 body is a bot-wall interstitial, not a document.

    Some bot walls answer *every* request with 200 and a few kilobytes of
    JavaScript that plants a cookie and reloads, instead of the 403 or
    vendor-branded challenge :func:`_detect_waf` recognises. Gazeta.ru does
    this: all 20 sampled articles came back "200 OK" as a 4 KB page titled
    "Document", which scored naively as twenty successful fetches of articles
    that merely happened to carry no photograph.

    Precision over recall, in keeping with ``_detect_waf``. All four must hold:

    * the body is tiny;
    * it sets a cookie from script;
    * it forces a navigation, so the cookie is the point of the page;
    * it contains no article furniture whatsoever — no image, no structured
      data, no links, no paragraphs.

    The last test is what makes this safe. A real article that happens to set a
    cookie and redirect still carries images, links or text, so it cannot match.
    """
    if len(html) > _JS_CHALLENGE_MAX_BYTES:
        return False
    low = html.lower()
    if "document.cookie" not in low:
        return False
    if not any(nav in low for nav in _JS_CHALLENGE_NAV):
        return False
    return not any(
        marker in low
        for marker in ("<img", "og:image", "ld+json", "<a ", "<p>", "<article")
    )


def _detect_waf(resp: httpx.Response) -> str | None:
    """Return the WAF/bot-management vendor if ``resp`` is a recognizable block
    or challenge, else None.

    Precision over recall — a signal requires either (a) a vendor header on a
    refusal response (or with that vendor's body marker), or (b) a
    challenge-page body marker unique to a vendor. A generic HTML body, a bare
    status code, or a feed merely *fronted* by a CDN does not qualify.
    Connection resets and plain timeouts carry no response, so they can't be
    attributed here and remain ``network_error`` → ``unreachable``.
    """
    # "Not found" is an answer, not a refusal. A WAF that blocks says 403/429/
    # 503 or serves a 200 challenge; it doesn't claim the page is missing. But
    # Imperva injects its _Incapsula_Resource script into ordinary pages,
    # 404s included, once a client has made a few requests — so guessing
    # /feed.xml, /atom.xml… on pap.pl labelled the site "blocked by Imperva"
    # when it simply has no feed.
    if resp.status_code in (404, 410):
        return None

    h = resp.headers
    server = (h.get("server") or "").lower()
    refusal = resp.status_code in _WAF_REFUSAL_CODES

    has_cf = "cf-ray" in h or "cf-mitigated" in h or "cloudflare" in server
    has_dd = "x-datadome" in h or "x-dd-b" in h
    has_ak = "akamaighost" in server or "x-akamai-transformed" in h
    has_imp = "x-iinfo" in h or "incapsula" in (h.get("x-cdn") or "").lower()
    has_suc = "x-sucuri-id" in h or "x-sucuri-block" in h
    any_vendor_hdr = has_cf or has_dd or has_ak or has_imp or has_suc

    # Sucuri's block header is unambiguous on its own.
    if has_suc:
        return "sucuri"

    # Only pay to decode the body when there's a reason to suspect a challenge.
    body = ""
    if any_vendor_hdr or refusal or "html" in (h.get("content-type") or "").lower():
        try:
            body = resp.text[:4000].lower()
        except Exception:
            body = ""

    def _marker(vendor: str) -> bool:
        return any(m in body for m in _WAF_BODY_MARKERS[vendor])

    if has_cf and (refusal or _marker("cloudflare")):
        return "cloudflare"
    if has_dd and (refusal or _marker("datadome")):
        return "datadome"
    if has_ak and (refusal or _marker("akamai")):
        return "akamai"
    if has_imp and (refusal or _marker("imperva")):
        return "imperva"
    # Body markers with no vendor header — specific enough to stand alone, so a
    # proxied or relabeled WAF is still caught. "akamai" excluded: its only
    # marker ("access denied") is too generic without the header.
    for vendor in ("cloudflare", "datadome", "imperva", "sucuri"):
        if _marker(vendor):
            return vendor
    # Unbranded 200-status cookie challenge. No vendor to name, so it reports
    # as "js-challenge"; the point is that this is a refusal, not a document.
    # Tested against the whole body, not the 4 KB `body` slice above, because
    # the size ceiling is half of what makes the fingerprint safe.
    if "html" in (h.get("content-type") or "").lower():
        try:
            full = resp.text
        except Exception:
            full = ""
        if full and looks_like_js_challenge(full):
            return "js-challenge"
    return None



def _not_found_kind(resp: httpx.Response) -> str | None:
    """"not_found" when a feed/sitemap URL simply isn't there, else None.

    Either an honest 404/410, or a soft 404: an ordinary HTML page served
    with 200 where a feed or sitemap should be (PAP's /rss.xml is a "Not
    found." page; SPA catch-alls return the homepage). Call only after
    _detect_waf has ruled out a challenge page, which is also HTML.
    """
    if resp.status_code in (404, 410):
        return "not_found"
    ctype = (resp.headers.get("content-type") or "").lower()
    if resp.status_code < 400 and "html" in ctype:
        return "not_found"
    return None


def summarise_discovery_errors(errors: list[str | None]) -> str | None:
    """One error kind for a site from every source discovery tried.

    "not_found" only when every attempt was not found; that is what earns
    the no_feed_found status. Otherwise the last real error wins, so a
    configured feed that refused us isn't masked by a guessed path that
    merely 404'd afterwards.
    """
    real = [e for e in errors if e is not None]
    if not real:
        return None
    if all(e == "not_found" for e in real):
        return "not_found"
    return [e for e in real if e != "not_found"][-1]

@dataclass
class RobotsDecision:
    fetched: bool
    text: str
    allowed_at_root: bool
    sitemap_urls: list[str]
    crawl_delay: float | None
    _parser: object | None = field(default=None, compare=False, repr=False)

    def can_fetch(self, url: str) -> bool:
        """Whether our UA may fetch ``url`` per this robots.txt.

        Fails OPEN: if robots.txt could not be read (``fetched`` is False —
        e.g. a WAF dropped it), we allow the fetch. We only ever block on a
        robots.txt we actually read that disallows our UA.
        """
        if not self.fetched:
            return True
        if self._parser is None:
            self._parser = Protego.parse(self.text)
        return self._parser.can_fetch(url, USER_AGENT)


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


def _guess_rss_urls(site_url: str) -> list[str]:
    base = site_url.rstrip("/")
    return [
        f"{base}/feed",
        f"{base}/feed/",
        f"{base}/rss",
        f"{base}/rss.xml",
        f"{base}/feed.xml",
        f"{base}/atom.xml",
        f"{base}/index.xml",
    ]


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


def _looks_like_xml_sitemap(body: str, content_type: str) -> bool:
    """Return True if ``body`` parses as the start of an XML sitemap / feed.

    SPA-style sites commonly answer 200 + HTML for *every* unknown path
    (their server-side router falls through to the SPA shell). A naive
    status-only check accepts those decoys as a "found" sitemap; the
    downstream XML parser then fails and the whole site is misreported as
    ``discovery_parse_error``. Probing the first ~200 bytes for an XML
    prologue or sitemap/feed root element is enough to reject the decoys
    cheaply, and the content-type header gives us a second, weaker check
    for servers that don't write a literal XML declaration.
    """
    head = body.lstrip()[:200].lower()
    if head.startswith(("<?xml", "<urlset", "<sitemapindex", "<feed", "<rss")):
        return True
    if "xml" in content_type.lower() and not head.startswith(("<!doctype", "<html")):
        return True
    return False


async def _try_guessed_sitemaps(client: httpx.AsyncClient, site: Site) -> tuple[str | None, str]:
    """Probe well-known sitemap paths until one returns actual XML.

    Uses GET rather than HEAD because the body sniff is what tells us a
    response is a real sitemap vs an SPA catch-all HTML decoy. Sitemaps are
    small enough that the extra payload is cheaper than misclassifying.
    """
    for url in _guess_sitemap_urls(site.url):
        try:
            r = await client.get(url, timeout=10.0, follow_redirects=True)
        except Exception:
            continue
        if r.status_code >= 400 or not r.content:
            continue
        if _looks_like_xml_sitemap(r.text, r.headers.get("content-type", "")):
            return url, "guess:sitemap"
    return None, "no_sitemap"


async def fetch_sitemap_articles(
    client: httpx.AsyncClient,
    site: Site,
    sitemap_url: str,
    window_days: int,
    max_articles: int,
) -> tuple[list[ArticleCandidate], str | None]:
    """Returns (articles, error_kind). See fetch_rss_articles for error_kind values."""
    candidates, root_err = await _walk_sitemap(client, sitemap_url, depth=0)
    if not candidates:
        return [], root_err  # propagate the first-level error if any

    candidates = dedupe_candidates(candidates)
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    filtered = [
        c for c in candidates
        if c.publication_date is None or c.publication_date >= cutoff
    ]
    filtered.sort(
        key=lambda c: c.publication_date or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return filtered[:max_articles], None


def dedupe_candidates(candidates: list[ArticleCandidate]) -> list[ArticleCandidate]:
    """Collapse repeated article URLs, merging metadata across the copies.

    The same article routinely turns up more than once in a single discovery
    source. CRHoy lists every recent story in both `sitemap-latest.xml`
    (<lastmod> only) and `sitemap-news.xml` (a full <news:news> block); Axios
    repeats URLs across its paginated sitemaps; a handful of RSS feeds carry
    the same story twice. Left unmerged we fetch the article once per copy and
    emit an article row for each, so the site's 20-article sample covers far
    fewer distinct stories than it looks like — Axios measured 7 in the
    2026-08-01 run, CRHoy 11 — and the site's article table shows each story
    twice, once headlined and once (the copy from the plain sitemap, which has
    no news:title) as a bare URL.

    First occurrence keeps its position; later copies only supply what it
    lacks. A copy carrying a news:title also wins the date and language: its
    news:publication_date is the article's real publication time, where the
    <lastmod> the plain-sitemap copy carries is merely the last edit.
    """
    by_url: dict[str, ArticleCandidate] = {}
    for cand in candidates:
        kept = by_url.get(cand.url)
        if kept is None:
            by_url[cand.url] = cand
            continue
        if cand.title and not kept.title:
            kept.title = cand.title
            kept.language = cand.language or kept.language
            kept.keywords = cand.keywords or kept.keywords
            if cand.publication_date is not None:
                kept.publication_date = cand.publication_date
        else:
            kept.publication_date = kept.publication_date or cand.publication_date
            kept.language = kept.language or cand.language
            kept.keywords = kept.keywords or cand.keywords
        kept.image_urls_from_sitemap = (
            kept.image_urls_from_sitemap or cand.image_urls_from_sitemap
        )
    return list(by_url.values())


async def _walk_sitemap(
    client: httpx.AsyncClient,
    url: str,
    depth: int,
    seen: set[str] | None = None,
) -> tuple[list[ArticleCandidate], str | None]:
    """Returns (candidates, error_kind). error_kind is only meaningful for the root call."""
    if seen is None:
        seen = set()
    if url in seen or depth > 2:
        return [], None
    seen.add(url)

    try:
        r = await client.get(url, timeout=15.0, follow_redirects=True)
    except (httpx.NetworkError, httpx.TimeoutException):
        return [], "network_error"
    except Exception:
        return [], "network_error"
    if r.status_code >= 400:
        waf = _detect_waf(r)
        return [], (f"waf:{waf}" if waf else _not_found_kind(r) or "http_error")

    body = r.content
    # Some sites serve .xml.gz sitemaps without setting Content-Encoding,
    # so httpx doesn't auto-decompress. Detect by URL suffix or gzip magic bytes.
    if url.endswith(".gz") or body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except OSError:
            return [], "parse_error"

    # A surprising number of sitemaps ship a stray leading newline, BOM, or
    # other whitespace before the XML declaration. lxml rejects that with
    # "XML declaration allowed only at the start of the document", so strip
    # leading whitespace/BOM before parsing (e.g. The Nation, Nigeria).
    body = body.lstrip(b"\xef\xbb\xbf \t\r\n")

    try:
        root = etree.fromstring(body)
    except etree.XMLSyntaxError:
        waf = _detect_waf(r)
        return [], (f"waf:{waf}" if waf else _not_found_kind(r) or "parse_error")

    tag = etree.QName(root.tag).localname

    if tag == "sitemapindex":
        out: list[ArticleCandidate] = []
        # Recurse the most-recently-modified children first. Paginated indexes
        # (Yoast `post-sitemap{N}.xml`, Tengrinews `…-sitemap-news-{N}.xml`)
        # list children oldest->newest, so the freshest articles live in the
        # LAST/highest-numbered child — taking children in document order would
        # only ever reach stale pages and the 30-day window would drop them all.
        # Sorting by <lastmod> desc makes the stable index URL resolve to live
        # content regardless of which numbered child is currently newest.
        # Children without a <lastmod> sort last but keep document order (stable
        # sort), so indexes that don't publish lastmod behave as before.
        sitemaps = root.findall("sm:sitemap", SITEMAP_NS)

        def _child_lastmod(sm: etree._Element) -> datetime:
            lm_el = sm.find("sm:lastmod", SITEMAP_NS)
            parsed = _parse_iso_date(lm_el.text.strip()) if lm_el is not None and lm_el.text else None
            return parsed or datetime.min.replace(tzinfo=UTC)

        sitemaps.sort(key=_child_lastmod, reverse=True)
        for sm in sitemaps[:15]:
            loc_el = sm.find("sm:loc", SITEMAP_NS)
            if loc_el is not None and loc_el.text:
                child_candidates, _ = await _walk_sitemap(client, loc_el.text.strip(), depth + 1, seen)
                out.extend(child_candidates)
        return out, None

    if tag == "urlset":
        return _parse_urlset(root), None

    return [], None


# File extensions that are never news articles. Some sitemaps list secondary
# resources (nested sitemaps, KML overlays, feeds, media, documents) inside
# <url><loc> elements rather than the article pages themselves; without this
# guard we'd treat e.g. `…/sitemap-1.xml` (Visual China) or `…/locations.kml`
# (PA Media) as articles, fetch them, and find no image.
_NON_ARTICLE_EXTENSIONS = (
    ".xml", ".xml.gz", ".gz", ".kml", ".kmz", ".json", ".rss", ".atom",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".zip",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".tif", ".tiff",
    ".mp4", ".mp3", ".mov", ".avi", ".css", ".js", ".ico",
)


def _is_probable_article_url(url: str) -> bool:
    """Reject URLs whose path ends in a non-article file extension."""
    path = urlparse(url).path.lower().rstrip("/")
    return not path.endswith(_NON_ARTICLE_EXTENSIONS)


# Marketing / tracking query parameters that publishers append to feed links but
# that don't identify the article. We strip them so we (a) fetch the canonical
# URL, and (b) honor robots.txt correctly — several publishers Disallow the
# tracking-param *variant* (e.g. `Disallow: *?ref=*` at SMH/The Age) while the
# clean article URL is allowed. Without stripping, every feed item looks blocked.
_TRACKING_PARAMS = frozenset({
    "ref", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "dclid", "gclsrc", "msclkid", "mc_cid", "mc_eid",
    "ocid", "cmpid", "spm", "icid", "ito", "app", "do",
})


def strip_tracking_params(url: str) -> str:
    """Remove known marketing/tracking query params, preserving any others.

    Only strips parameters that never identify the article (utm_*, ref, fbclid,
    …). Parameters a site genuinely needs for routing (e.g. ?id=123) are kept.
    """
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    return urlunsplit(parts._replace(query=urlencode(kept)))


def _parse_urlset(root: etree._Element) -> list[ArticleCandidate]:
    out: list[ArticleCandidate] = []
    for url_el in root.findall("sm:url", SITEMAP_NS):
        loc_el = url_el.find("sm:loc", SITEMAP_NS)
        if loc_el is None or not loc_el.text:
            continue
        url = strip_tracking_params(loc_el.text.strip())
        if not _is_probable_article_url(url):
            continue
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
                title = _clean_title(t_el.text)
            lang_el = news_el.find("news:publication/news:language", SITEMAP_NS)
            if lang_el is not None and lang_el.text:
                language = lang_el.text.strip()
            kw_el = news_el.find("news:keywords", SITEMAP_NS)
            if kw_el is not None and kw_el.text:
                keywords = [k.strip() for k in kw_el.text.split(",") if k.strip()]

        # Fall back to <lastmod> when the entry has no news:publication_date.
        # Without this, sites like Heute (lastmod-only sitemaps, ascending file
        # order) sort all candidates to datetime.min and we take the oldest N.
        if pub_date is None:
            lastmod_el = url_el.find("sm:lastmod", SITEMAP_NS)
            if lastmod_el is not None and lastmod_el.text:
                pub_date = _parse_iso_date(lastmod_el.text.strip())

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


# Some feeds (e.g. Correio da Manhã) wrap <title> text in a CDATA section that
# feedparser hands back verbatim rather than unwrapping, so the literal
# "<![CDATA[ ... ]]>" ends up in the title. Strip it defensively.
_CDATA_RE = re.compile(r"^\s*<!\[CDATA\[(.*?)\]\]>\s*$", re.DOTALL)


def _clean_title(title: str | None) -> str | None:
    if title is None:
        return None
    m = _CDATA_RE.match(title)
    cleaned = m.group(1) if m else title
    cleaned = cleaned.strip()
    return cleaned or None


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
) -> tuple[list[ArticleCandidate], str | None]:
    """Fetch and parse an RSS/Atom feed, with one retry on transient failure.

    Returns (articles, error_kind). error_kind is one of:
      None             — fetched and parsed successfully
      "network_error"  — DNS/connection/timeout/TLS failure (publisher unreachable)
      "http_error"     — got a response, status >= 400 (e.g. 403 from a WAF)
      "parse_error"    — got 2xx but body wasn't a parseable feed

    A single retry runs on network_error and http_error after a small
    backoff. Brief Fastly/Cloudflare blips on bot-aware feeds (e.g. a
    momentary 429 or a Fastly origin error that clears in seconds) used
    to permanently misclassify the publisher as ``discovery_blocked`` for
    the whole month-long crawl interval; the retry rescues those without
    bloating the per-site budget. ``parse_error`` is not retried — a
    server returning HTML or malformed XML once will do the same on
    retry, and the response usually carries useful diagnostic info that
    we want to log immediately.
    """
    articles, err = await _fetch_rss_once(client, feed_url, window_days, max_articles)
    if err in ("network_error", "http_error"):
        await asyncio.sleep(_RSS_RETRY_BACKOFF_S)
        articles, err = await _fetch_rss_once(
            client, feed_url, window_days, max_articles, attempt=2,
        )
    return articles, err


async def _fetch_rss_once(
    client: httpx.AsyncClient,
    feed_url: str,
    window_days: int,
    max_articles: int,
    *,
    attempt: int = 1,
) -> tuple[list[ArticleCandidate], str | None]:
    """One HTTP attempt at fetching ``feed_url`` and parsing it as RSS/Atom.

    Failure paths log the response status / content-type / body preview so
    we can diagnose ``discovery_blocked`` events post-hoc — without that
    detail, all WAF-related rejections look identical in the parquet row
    even though "blocked 403" and "rate-limited 429" want different fixes.
    """
    tag = f"[yellow]rss attempt {attempt}[/yellow]" if attempt > 1 else "[yellow]rss[/yellow]"
    try:
        r = await client.get(feed_url, timeout=20.0, follow_redirects=True)
    except (httpx.NetworkError, httpx.TimeoutException) as e:
        _console.print(f"  {tag} network_error {feed_url}  {type(e).__name__}: {str(e)[:80]}")
        return [], "network_error"
    except Exception as e:
        _console.print(f"  {tag} network_error {feed_url}  {type(e).__name__}: {str(e)[:80]}")
        return [], "network_error"
    if r.status_code >= 400:
        ct = (r.headers.get("content-type") or "").split(";")[0].strip()
        snippet = r.text[:120].replace("\n", " ").replace("\r", "")
        waf = _detect_waf(r)
        kind = f"waf:{waf}" if waf else _not_found_kind(r) or "http_error"
        _console.print(f"  {tag} {kind} {r.status_code} {feed_url}  ct={ct!r}  body={snippet!r}")
        return [], kind
    if not r.content:
        _console.print(f"  {tag} parse_error {feed_url}  (empty body, status {r.status_code})")
        return [], "parse_error"
    parsed = feedparser.parse(r.content)
    if parsed.bozo and not parsed.entries:
        ct = (r.headers.get("content-type") or "").split(";")[0].strip()
        snippet = r.text[:120].replace("\n", " ").replace("\r", "")
        bozo = getattr(parsed, "bozo_exception", None)
        waf = _detect_waf(r)
        kind = f"waf:{waf}" if waf else _not_found_kind(r) or "parse_error"
        _console.print(
            f"  {tag} {kind} {feed_url}  ct={ct!r}  "
            f"bozo={type(bozo).__name__ if bozo else '?'}  body={snippet!r}"
        )
        return [], kind

    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    out: list[ArticleCandidate] = []
    for entry in parsed.entries:
        link = entry.get("link")
        if link:
            link = strip_tracking_params(link)
        if not link or not _is_probable_article_url(link):
            continue
        pub = _entry_datetime(entry)
        if pub is not None and pub < cutoff:
            continue
        out.append(
            ArticleCandidate(
                url=link,
                publication_date=pub,
                title=_clean_title(entry.get("title")),
                language=None,
                keywords=[],
                # RSS commonly carries the lead image inline via Media RSS or
                # enclosures. Picking it up here lets us still measure the
                # image when the article HTML itself is WAF-blocked from the
                # crawl runner — fetch_article will fall back to this list.
                image_urls_from_sitemap=_images_from_rss_entry(entry),
            )
        )
    out = dedupe_candidates(out)
    out.sort(
        key=lambda c: c.publication_date or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return out[:max_articles], None


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


def _images_from_rss_entry(entry) -> list[str]:
    """Collect lead-image URLs declared in an RSS/Atom entry.

    Handles Media RSS (``media:content``, ``media:thumbnail``) and standard
    ``<enclosure>`` elements. Filters enclosures to image MIME types so we
    don't pick up audio/video. Order preserves the feed's order and
    deduplicates.
    """
    out: list[str] = []

    for item in entry.get("media_content") or []:
        url = item.get("url")
        if not url:
            continue
        medium = item.get("medium")
        mtype = (item.get("type") or "").lower()
        # media:content omits medium/type sometimes; assume image if neither
        # is set, otherwise require an image hint.
        if medium not in (None, "", "image") and not mtype.startswith("image"):
            continue
        out.append(url)

    for item in entry.get("media_thumbnail") or []:
        url = item.get("url")
        if url:
            out.append(url)

    for item in entry.get("enclosures") or []:
        url = item.get("href") or item.get("url")
        mtype = (item.get("type") or "").lower()
        if url and mtype.startswith("image"):
            out.append(url)

    # Last resort: parse <img src=…> from content:encoded / description HTML.
    # Some feeds (Neue.at) carry images inline rather than via Media RSS or
    # enclosures. Skips data: URIs and SVGs to avoid sparklines/spacers.
    html_blobs: list[str] = []
    for c in entry.get("content") or []:
        v = c.get("value") if isinstance(c, dict) else None
        if v:
            html_blobs.append(v)
    if entry.get("summary"):
        html_blobs.append(entry["summary"])
    for html in html_blobs:
        for src in re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html, flags=re.I):
            s = src.strip()
            sl = s.lower()
            if not s or sl.startswith("data:") or sl.endswith(".svg"):
                continue
            out.append(s)

    seen: set[str] = set()
    deduped: list[str] = []
    for url in out:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


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


def status_for_empty_discovery(strategy: str, discovery_error: str | None) -> str:
    """Pick a site status when discovery returned zero article candidates.

    Distinguishes "the source actively refused us" (HTTP 4xx, parse error)
    from "the source was reachable but had no articles in window". Both
    used to collapse to ``no_articles_found``, which sounds like a
    crawler bug when in fact the source itself is blocking or broken.
    """
    if strategy == "unreachable":
        return "unreachable"
    if discovery_error and discovery_error.startswith("waf:"):
        return "blocked_by_waf"
    if discovery_error == "http_error":
        return "discovery_blocked"
    if discovery_error == "parse_error":
        return "discovery_parse_error"
    if discovery_error == "not_found":
        # Every feed/sitemap address we tried simply wasn't there.
        return "no_feed_found"
    if discovery_error == "network_error":
        # Upstream usually already classified these as "unreachable", but
        # handle the lingering case too.
        return "unreachable"
    return "no_articles_found"


async def discover(
    client: httpx.AsyncClient, site: Site
) -> tuple[RobotsDecision, str | None, str, list[ArticleCandidate], str | None]:
    """Run the full discovery pipeline.

    Returns (robots, source_url, strategy, articles, discovery_error).

    discovery_error summarises every attempted source when no articles were
    harvested (see summarise_discovery_errors) — one of "waf:<vendor>" (a
    recognized WAF/bot challenge, vendor-qualified so callers can report who
    blocked us), "http_error", "parse_error", "network_error", "not_found"
    (only when every source was a 404/410 or soft 404), or None. Lets
    callers distinguish "the source 4xx'd" from "the source was reachable but
    had no candidates in window",
    which look identical to the user otherwise.

    Source priority — first to yield >=1 article wins:
      1. config:rss          — RSS URL(s) configured in the site YAML
      2. html:rss_link       — RSS URL(s) advertised in homepage <link rel="alternate">
      3. config:sitemap, etc — sitemap chain (config/robots/guessed)

    Robots policy — we honor robots.txt for EVERY site, including ones with a
    publisher-configured RSS feed. Metawatch measures robots / AI-policy
    compliance, so crawling what a readable robots.txt disallows for our UA
    would undermine the project's own premise. Two safeguards stop this from
    locking us out of sites we are genuinely allowed to crawl:

      * Fail-open: ``fetch_robots`` / ``RobotsDecision.can_fetch`` allow the
        fetch when /robots.txt can't be read (some WAFs 403 or drop it), so a
        publisher who merely hides robots.txt is still crawled.
      * Feed-first for configured RSS: we fetch the FEED before robots.txt, so
        a WAF that cascade-blocks the IP after a robots request (e.g. tass.ru)
        can't cost us the feed we were invited to crawl. We then drop any
        individual article URLs that the robots.txt we read disallows.

    Sitemap/guess discovery still fetches robots up front (the sitemap URLs
    come from it). Configured-RSS sites fetch it lazily, after the first feed.
    """
    rss_configured = bool(site.discovery.rss_urls)

    robots: RobotsDecision | None = None
    if not rss_configured:
        robots = await fetch_robots(client, site)
        if robots.fetched and not robots.allowed_at_root:
            return robots, None, "robots_disallow", [], None

    window_days = site.sample.window_days
    max_articles = site.sample.max_articles

    attempts = 0
    network_errors = 0
    errors: list[str | None] = []

    async def _robots_gate(
        source_url: str, strategy: str, articles: list[ArticleCandidate]
    ):
        """Honor robots for freshly discovered articles, fetching robots.txt
        lazily (feed-first). Returns a discover() result tuple, or None to keep
        trying other sources when the read robots.txt disallows everything."""
        nonlocal robots
        if robots is None:
            robots = await fetch_robots(client, site)
        if robots.fetched and not robots.allowed_at_root:
            return robots, None, "robots_disallow", [], None
        allowed = [a for a in articles if robots.can_fetch(a.url)]
        if allowed:
            return robots, source_url, strategy, allowed, None
        # A readable robots.txt disallowed every URL the feed offered.
        return robots, None, "robots_disallow", [], None

    # Two flavours of "the publisher told us where to look":
    #
    #   rss_configured     — discovery.rss_urls is set. Use the homepage
    #                        <link rel="alternate"> scan as a fallback if
    #                        the configured URL goes stale (this saved us
    #                        when kyivindependent.com moved their feed
    #                        from /rss/ to /news-archive/rss/ and the
    #                        homepage <link> still pointed at the right
    #                        place). One extra HTTP request, much higher
    #                        signal-to-noise than blind guessing.
    #
    #   sitemap_configured — discovery.sitemap_urls or picture_sitemap_url
    #                        is set. RSS scanning is wasted: the publisher
    #                        has explicitly named a sitemap, scanning the
    #                        homepage for non-existent RSS feeds is the
    #                        14-wasted-requests Gazeta case.
    #
    # Blind guessing (the 7 hardcoded RSS paths, the 7 hardcoded sitemap
    # paths) only runs when neither flag is set — for sites with no
    # explicit configuration at all, where guessing is genuinely useful
    # as discovery aid.
    sitemap_configured = bool(
        site.discovery.sitemap_urls or site.discovery.picture_sitemap_url
    )
    has_explicit_source = rss_configured or sitemap_configured

    for url in site.discovery.rss_urls:
        attempts += 1
        articles, err = await fetch_rss_articles(client, url, window_days, max_articles)
        if articles:
            return await _robots_gate(url, "config:rss", articles)
        errors.append(err)
        if err == "network_error":
            network_errors += 1

    # Homepage <link rel="alternate"> scan: useful when the publisher has
    # RSS configured but the configured URL has gone stale, OR when no
    # source is configured at all.
    if rss_configured or not has_explicit_source:
        for url in await discover_rss_links_from_homepage(client, site.homepage or site.url):
            attempts += 1
            articles, err = await fetch_rss_articles(client, url, window_days, max_articles)
            if articles:
                return await _robots_gate(url, "html:rss_link", articles)
            errors.append(err)
            if err == "network_error":
                network_errors += 1

    # Blind path guessing: only when the publisher has named nothing at all.
    if not has_explicit_source:
        for url in _guess_rss_urls(site.homepage or site.url):
            attempts += 1
            articles, err = await fetch_rss_articles(client, url, window_days, max_articles)
            if articles:
                return await _robots_gate(url, "guess:rss", articles)
            errors.append(err)
            if err == "network_error":
                network_errors += 1

    # RSS-configured sites that found nothing fall through to the sitemap chain;
    # fetch robots now (feed attempts are already done, so feed-first holds) so
    # robots.sitemap_urls is available and any root disallow is honored.
    if robots is None:
        robots = await fetch_robots(client, site)
        if robots.fetched and not robots.allowed_at_root:
            return robots, None, "robots_disallow", [], None

    sitemap_url, strategy = pick_sitemap(robots.sitemap_urls, site)
    if not sitemap_url and not has_explicit_source:
        sitemap_url, strategy = await _try_guessed_sitemaps(client, site)
    if not sitemap_url:
        # No sitemap to try. If every RSS attempt was a network error, the
        # publisher is effectively unreachable from here.
        if attempts > 0 and network_errors == attempts:
            return robots, None, "unreachable", [], summarise_discovery_errors(errors)
        return robots, None, strategy, [], summarise_discovery_errors(errors)

    attempts += 1
    articles, err = await fetch_sitemap_articles(
        client, site, sitemap_url, window_days, max_articles
    )
    if articles:
        allowed = [a for a in articles if robots.can_fetch(a.url)]
        if allowed:
            return robots, sitemap_url, strategy, allowed, None
        # A readable robots.txt disallowed every URL in the sitemap.
        return robots, None, "robots_disallow", [], None
    errors.append(err)
    if err == "network_error":
        network_errors += 1
    if attempts > 0 and network_errors == attempts:
        return robots, sitemap_url, "unreachable", [], summarise_discovery_errors(errors)
    return robots, sitemap_url, strategy, [], summarise_discovery_errors(errors)
