"""Article fetch and image-URL extraction.

The "lead image" — the one photograph an article is really built around — is
picked from a priority chain. We try each step in order and stop at the first
non-empty result:

  1. JSON-LD NewsArticle/Article ``image`` (the publisher's own declaration).
  2. <meta property="og:image"> / og:image:url (Open Graph).
  3. <meta name="twitter:image">.
  4. <link rel="image_src"> (older Yahoo/Facebook convention).
  5. Schema.org microdata: <*itemprop="image">.
  6. <image:loc> from the site's news sitemap, if discovery supplied one.
  7. DOM walk: the largest <img> (incl. <picture><source srcset>) inside a
     <figure>, <article> or <main>, filtered by minimum dimensions and known
     non-content URL patterns (avatars, logos, sprites, ad networks).

Sitemap-supplied images are *not* short-circuiting any more: we always fetch
the article HTML so the page's own JSON-LD, og:image and (Phase 3) trust
signals are available for analysis.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser, Node

from .discovery import ArticleCandidate
from .optout import scan_robots_directives

MIN_DIM = 200

# URL substrings that almost always indicate a non-lead image. Case-insensitive.
NON_LEAD_URL_PATTERNS = (
    "/avatar", "/avatars/", "byline", "headshot",
    "/logo", "logo.", "/icon", "icon-", "/icons/",
    "sprite",
    "/ad/", "/ads/", "doubleclick.net",
    "googlesyndication", "taboola", "outbrain",
    "/pixel", "tracking.gif",
)

# Lazy-load attribute names in priority order — first non-empty wins.
LAZY_ATTRS = ("data-src", "data-lazy-src", "data-original", "data-hi-res-src")
LAZY_SRCSET_ATTRS = ("data-srcset", "data-lazy-srcset")


@dataclass
class ArticleResult:
    candidate: ArticleCandidate
    http_status: int
    image_urls: list[str]
    jsonld_news_article: str | None  # raw JSON string, may be empty
    # noai/noimageai/noml tokens harvested from the article's HTTP response
    # X-Robots-Tag header and any <meta name="robots"> on the page. Treated
    # as opt-out signals attached to every image on this article.
    noai_tokens: list[str] = None  # type: ignore[assignment]  # init in __post_init__
    # Per-page TDMRep declaration: <meta name="tdm-reservation" content="1">.
    # Parsed as int per the W3C TDMRep spec — 1 = reserved, 0 = unreserved;
    # None when the meta tag is absent. The per-page mechanism is one of two
    # ways the IPTC opt-out best-practices doc recommends for TDMRep, the
    # other being the site-wide /.well-known/tdmrep.json file (already probed
    # in optout.py).
    tdm_reservation: int | None = None

    def __post_init__(self) -> None:
        if self.noai_tokens is None:
            self.noai_tokens = []


async def fetch_article(
    client: httpx.AsyncClient, candidate: ArticleCandidate
) -> ArticleResult:
    """Fetch the article HTML and extract its lead image plus JSON-LD payload."""
    try:
        r = await client.get(candidate.url, timeout=15.0, follow_redirects=True)
    except Exception:
        # Sitemap may still have an image for us even if the article fetch failed.
        if candidate.image_urls_from_sitemap:
            return ArticleResult(candidate, 0, [candidate.image_urls_from_sitemap[0]], None)
        return ArticleResult(candidate, 0, [], None)

    if r.status_code >= 400 or not r.content:
        if candidate.image_urls_from_sitemap:
            return ArticleResult(candidate, r.status_code, [candidate.image_urls_from_sitemap[0]], None)
        return ArticleResult(candidate, r.status_code, [], None)

    parser = HTMLParser(r.text)
    base_url = r.url.human_repr() if hasattr(r.url, "human_repr") else str(r.url)
    sitemap_image = (
        candidate.image_urls_from_sitemap[0] if candidate.image_urls_from_sitemap else None
    )
    main_image = _extract_main_image(parser, base_url, sitemap_image)
    jsonld = _extract_news_article_jsonld(parser)
    noai = _collect_noai_tokens(r, parser)
    tdm_reservation = _extract_tdm_reservation(parser)
    return ArticleResult(
        candidate, r.status_code, [main_image] if main_image else [], jsonld, noai,
        tdm_reservation=tdm_reservation,
    )


def _extract_tdm_reservation(parser: HTMLParser) -> int | None:
    """Read the W3C TDMRep per-page meta tag, if present.

    Spec: ``<meta name="tdm-reservation" content="0|1">``. Returns the int
    value, or None when the tag is absent or unparseable. We tolerate the
    rare publishers who wrap the value in quotes/whitespace.
    """
    for m in parser.css('meta[name="tdm-reservation"], meta[name="TDM-Reservation"]'):
        raw = (m.attributes.get("content") or "").strip().strip('"').strip("'")
        if raw in ("0", "1"):
            return int(raw)
    return None


def _collect_noai_tokens(r: httpx.Response, parser: HTMLParser) -> list[str]:
    """Gather noai/noimageai tokens from response headers and <meta robots>."""
    tokens: list[str] = []
    seen: set[str] = set()

    def _add(values: list[str]) -> None:
        for t in values:
            if t not in seen:
                seen.add(t)
                tokens.append(t)

    # httpx exposes repeated headers via `get_list`; older versions need a manual walk.
    header_values = r.headers.get_list("x-robots-tag") if hasattr(r.headers, "get_list") else [
        v for k, v in r.headers.items() if k.lower() == "x-robots-tag"
    ]
    for hv in header_values:
        _add(scan_robots_directives(hv))

    for m in parser.css('meta[name="robots"], meta[name="ROBOTS"]'):
        _add(scan_robots_directives(m.attributes.get("content")))

    return tokens


# ─── Top-level extractor ────────────────────────────────────────────────────


def _extract_main_image(
    parser: HTMLParser,
    base_url: str,
    sitemap_image: str | None = None,
) -> str | None:
    """Return the lead image URL or None, walking the priority chain."""
    for finder in (
        _from_jsonld,
        _from_og_image,
        _from_twitter_image,
        _from_link_image_src,
        _from_itemprop_image,
    ):
        found = finder(parser)
        if found:
            return urljoin(base_url, found)

    if sitemap_image:
        return sitemap_image

    return _from_dom_walk(parser, base_url)


# ─── Per-strategy extractors ────────────────────────────────────────────────


def _from_jsonld(parser: HTMLParser) -> str | None:
    for node in parser.css('script[type="application/ld+json"]'):
        text = node.text() or ""
        for img in _images_from_jsonld(text):
            return img
    return None


def _from_og_image(parser: HTMLParser) -> str | None:
    for og in parser.css('meta[property="og:image"], meta[property="og:image:url"]'):
        content = (og.attributes.get("content") or "").strip()
        if content:
            return content
    return None


def _from_twitter_image(parser: HTMLParser) -> str | None:
    for tw in parser.css('meta[name="twitter:image"], meta[name="twitter:image:src"]'):
        content = (tw.attributes.get("content") or "").strip()
        if content:
            return content
    return None


def _from_link_image_src(parser: HTMLParser) -> str | None:
    for ln in parser.css('link[rel="image_src"]'):
        href = (ln.attributes.get("href") or "").strip()
        if href:
            return href
    return None


def _from_itemprop_image(parser: HTMLParser) -> str | None:
    # <meta itemprop="image" content="...">
    for m in parser.css('meta[itemprop="image"]'):
        c = (m.attributes.get("content") or "").strip()
        if c:
            return c
    # <img itemprop="image" src="..."> or any child of <... itemprop="image">
    for n in parser.css('[itemprop="image"]'):
        if n.tag == "img":
            src = _img_real_src(n)
            if src:
                return src
        else:
            inner = n.css_first("img")
            if inner is not None:
                src = _img_real_src(inner)
                if src:
                    return src
    return None


def _from_dom_walk(parser: HTMLParser, base_url: str) -> str | None:
    """Largest non-junk image inside the article's main content containers."""
    best_url: str | None = None
    best_width: int = 0
    seen: set[str] = set()
    for selector in ("article figure img", "main figure img", "article img", "main img", "figure img"):
        for img in parser.css(selector):
            candidate = _img_best_candidate(img)
            if candidate is None:
                continue
            url, width = candidate
            abs_url = urljoin(base_url, url)
            if abs_url in seen or _looks_like_non_lead(abs_url):
                continue
            seen.add(abs_url)
            if width < MIN_DIM:
                continue
            if width > best_width:
                best_width, best_url = width, abs_url
    return best_url


# ─── Helpers ────────────────────────────────────────────────────────────────


def _images_from_jsonld(raw: str) -> list[str]:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    out: list[str] = []
    _walk_jsonld_for_images(data, out)
    return out


def _type_set(t: object) -> set[str]:
    """Normalise a JSON-LD @type (str | list | missing) into a set of strings."""
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return {x for x in t if isinstance(x, str)}
    return set()


def _is_article_type(t: str) -> bool:
    """True for schema.org Article, NewsArticle, or any NewsArticle subtype.

    schema.org defines several NewsArticle subtypes — AnalysisNewsArticle,
    AskPublicNewsArticle, BackgroundNewsArticle, OpinionNewsArticle,
    ReportageNewsArticle, ReviewNewsArticle — all of which carry the same
    lead-image semantics. Rather than enumerate them, we accept anything
    ending in "NewsArticle" so future subtypes work without a code change.
    """
    return t == "Article" or t.endswith("NewsArticle")


def _walk_jsonld_for_images(node: object, out: list[str]) -> None:
    if isinstance(node, dict):
        types = _type_set(node.get("@type"))
        if "ImageObject" in types or any(_is_article_type(x) for x in types):
            img = node.get("image")
            _collect_image_field(img, out)
        for v in node.values():
            _walk_jsonld_for_images(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_jsonld_for_images(v, out)


def _collect_image_field(node: object, out: list[str]) -> None:
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        u = node.get("url") or node.get("contentUrl")
        if isinstance(u, str):
            out.append(u)
    elif isinstance(node, list):
        for v in node:
            _collect_image_field(v, out)


def _img_real_src(img: Node) -> str | None:
    """Return the real image URL from an <img>, preferring lazy-load attrs.

    Modern news sites overwhelmingly leave ``src`` empty (or set it to a
    blurred placeholder / 1×1 GIF) and put the actual URL in a data-* attr.
    """
    for attr in LAZY_ATTRS:
        v = (img.attributes.get(attr) or "").strip()
        if v and not _looks_like_placeholder(v):
            return v
    src = (img.attributes.get("src") or "").strip()
    if src and not _looks_like_placeholder(src):
        return src
    return None


def _img_best_candidate(img: Node) -> tuple[str, int] | None:
    """Return (url, intrinsic_width) for the largest variant of an <img>.

    Considers srcset on the <img> itself, <picture><source srcset>, the lazy
    data-srcset attrs, and finally the bare src/data-src as a last resort.
    Width is taken from the ``Nw`` descriptor in srcset, or from the
    ``width`` attribute, or defaults to MIN_DIM when unknown.
    """
    best: tuple[str, int] | None = None
    for url, width in _iter_srcset_candidates(img):
        if best is None or width > best[1]:
            best = (url, width)
    parent = img.parent
    if parent is not None and parent.tag == "picture":
        for source in parent.css("source"):
            for url, width in _iter_srcset_candidates(source):
                if best is None or width > best[1]:
                    best = (url, width)
    if best is not None:
        return best
    real = _img_real_src(img)
    if not real:
        return None
    width = _attr_int(img, "width") or MIN_DIM
    return real, width


_SRCSET_ITEM_RE = re.compile(r"\s*([^,\s]+)(?:\s+([0-9.]+)(w|x))?\s*,?")


def _iter_srcset_candidates(node: Node) -> list[tuple[str, int]]:
    """Yield (url, width) entries from a node's srcset / data-srcset attrs."""
    out: list[tuple[str, int]] = []
    for attr in ("srcset", *LAZY_SRCSET_ATTRS):
        raw = node.attributes.get(attr)
        if not raw:
            continue
        for url, num, unit in _SRCSET_ITEM_RE.findall(raw):
            if not url:
                continue
            if _looks_like_placeholder(url):
                continue
            if unit == "w" and num:
                try:
                    width = int(float(num))
                except ValueError:
                    width = 0
            else:
                # density descriptor (2x) or none — we have no width info.
                width = 0
            out.append((url, width))
    return out


def _attr_int(node: Node, attr: str) -> int:
    v = node.attributes.get(attr)
    if not v:
        return 0
    try:
        return int(float(v))
    except ValueError:
        return 0


def _looks_like_placeholder(url: str) -> bool:
    """True for known empty/placeholder/blur-up patterns."""
    u = url.lower().strip()
    if not u:
        return True
    if u.startswith("data:"):
        # SVG shimmers and 1px PNG placeholders.
        return True
    if u.endswith(".svg"):
        return True
    return False


def _looks_like_non_lead(url: str) -> bool:
    """True for URLs that almost certainly aren't the article's lead image."""
    u = url.lower()
    host = urlparse(u).hostname or ""
    if any(p in host for p in ("doubleclick.net", "googlesyndication", "taboola", "outbrain")):
        return True
    return any(p in u for p in NON_LEAD_URL_PATTERNS)


def _extract_news_article_jsonld(parser: HTMLParser) -> str | None:
    for node in parser.css('script[type="application/ld+json"]'):
        text = node.text() or ""
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        if _has_article_type(data):
            return text
    return None


def _has_article_type(node: object) -> bool:
    """Recursively test whether any node declares an article @type.

    Matches Article, NewsArticle, and any *NewsArticle subtype (see
    _is_article_type).
    """
    if isinstance(node, dict):
        if any(_is_article_type(x) for x in _type_set(node.get("@type"))):
            return True
        for v in node.values():
            if _has_article_type(v):
                return True
    elif isinstance(node, list):
        for v in node:
            if _has_article_type(v):
                return True
    return False
