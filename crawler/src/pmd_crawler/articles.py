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
from html import unescape
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
    # Headline read from the article HTML, used only when discovery didn't
    # supply one (see _extract_title). None when the fetch failed or the page
    # carries no usable headline.
    title: str | None = None

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
        title=_extract_title(parser, jsonld, base_url),
    )


# Separators publishers put between the headline and the site name in <title>
# ("Headline | CR Hoy", "Headline - Trouw"). Split from the right, and only
# where what follows is recognisably the site's own name (see
# _strip_site_suffix) — headlines contain these characters too.
_TITLE_SEPARATOR_RE = re.compile(r"\s+[|–—‒·•]\s+|\s+-\s+")


def _extract_title(
    parser: HTMLParser, jsonld: str | None, base_url: str
) -> str | None:
    """Read the article headline from the page itself.

    Discovery only learns a title when the source hands one over: <news:title>
    in a news sitemap, or an RSS entry title. Sites on a plain lastmod-only
    sitemap (iza, and most of the 597 untitled rows in the 2026-08-01 run)
    arrive with title=None, and the site's article table then falls back to
    printing the raw URL. The article HTML is already in hand here, so take
    the headline from it.

    Priority: JSON-LD ``headline`` (the publisher's own declaration, and
    already parsed out for the metadata columns) -> og:title -> twitter:title
    -> <title>. The <title> value gets a trailing site name trimmed; the
    earlier sources rarely carry one.
    """
    headline = _headline_from_jsonld(jsonld)
    if headline:
        return headline

    for selector in (
        'meta[property="og:title"]',
        'meta[name="og:title"]',
        'meta[name="twitter:title"]',
    ):
        for node in parser.css(selector):
            value = _clean(node.attributes.get("content"))
            if value:
                return value

    node = parser.css_first("title")
    value = _clean(node.text() if node else None)
    if not value:
        return None
    return _strip_site_suffix(value, parser, base_url)


def _strip_site_suffix(title: str, parser: HTMLParser, base_url: str) -> str:
    """Drop a trailing site name from a <title>, if that's what the tail is.

    Trimming on the separator alone would eat real headline text — "Trump meets
    Xi - live updates" ends exactly like "Headline - Trouw". So the tail is only
    removed when it matches something that identifies the site: og:site_name,
    <meta name="application-name">, or a label of the hostname. Comparison
    ignores case, spaces and punctuation, which is what makes "CR Hoy" match
    crhoy.com and "The Guardian" match theguardian.com.

    Stripping repeats while the tail keeps matching: Hindustan Times ships
    "… | Hindustan Times | Hindustan Times".
    """
    names: list[str] = []
    for selector in (
        'meta[property="og:site_name"]',
        'meta[name="og:site_name"]',
        'meta[name="application-name"]',
    ):
        for node in parser.css(selector):
            value = _clean(node.attributes.get("content"))
            if value:
                names.append(value)
    host = (urlparse(base_url).hostname or "").lower()
    names.extend(host.split("."))
    normalised_names = {_norm(n) for n in names if _norm(n)}
    if not normalised_names:
        return title

    while True:
        separators = list(_TITLE_SEPARATOR_RE.finditer(title))
        if not separators:
            return title
        last = separators[-1]
        head, tail = title[: last.start()].strip(), title[last.end():].strip()
        if not head or _norm(tail) not in normalised_names:
            return title
        title = head


def _norm(value: str) -> str:
    """Lowercase and strip everything but alphanumerics, for name comparison."""
    return re.sub(r"[^0-9a-z]", "", value.lower())


def _headline_from_jsonld(raw: str | None) -> str | None:
    """Pull ``headline`` off the first article-typed node in the JSON-LD.

    The result is HTML-unescaped: <script> content is not entity-decoded by the
    parser, so publishers who build their JSON-LD from escaped CMS strings hand
    us headlines like "Ser extremamente vulner&#225;vel" (Correio). The other
    sources come from attributes or element text, which arrive already decoded.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    headline = _walk_jsonld_for_headline(data)
    return _clean(unescape(headline)) if headline else None


def _walk_jsonld_for_headline(node: object) -> str | None:
    if isinstance(node, dict):
        if any(_is_article_type(x) for x in _type_set(node.get("@type"))):
            headline = _clean(node.get("headline"))
            if headline:
                return headline
        for v in node.values():
            found = _walk_jsonld_for_headline(v)
            if found:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _walk_jsonld_for_headline(v)
            if found:
                return found
    return None


def _clean(value: object) -> str | None:
    """Collapse whitespace in a candidate title; None unless it survives."""
    if not isinstance(value, str):
        return None
    return re.sub(r"\s+", " ", value).strip() or None


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

    headline = _from_headline_adjacent(parser, base_url)
    if headline:
        return headline

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


def _nearest_anchor(node: Node) -> Node | None:
    """Climb up to a handful of ancestors to find an enclosing <a>, if any."""
    n = node.parent
    for _ in range(6):
        if n is None:
            return None
        if n.tag == "a":
            return n
        n = n.parent
    return None


def _links_to_other_article(anchor: Node | None, base_url: str) -> bool:
    """True when an enclosing <a> points to a *different* page than this one.

    Old-school layouts (e.g. Baltic Times) place a grid of related-article
    thumbnails in the same column as the <h1>. Each such thumbnail is wrapped
    in an <a> linking to another article, so excluding cross-links is what
    separates the page's own lead image from its neighbours' thumbnails.
    Self-links, ``#`` anchors, empty/JS hrefs are treated as non-cross-links.
    """
    if anchor is None:
        return False
    href = (anchor.attributes.get("href") or "").strip()
    if not href or href == "#" or href.lower().startswith("javascript:"):
        return False
    target = urlparse(urljoin(base_url, href)).path.rstrip("/")
    here = urlparse(base_url).path.rstrip("/")
    return target != here


def _from_headline_adjacent(parser: HTMLParser, base_url: str) -> str | None:
    """Lead image sitting next to the article's <h1> headline.

    A fallback for pages that carry no og:image / JSON-LD / itemprop image and
    use a non-semantic (plain ``<div>``) layout the generic DOM walk misses.
    Starting from the headline, we widen through its ancestors and take the
    first usable image — skipping placeholders, logos/icons, and thumbnails
    that are wrapped in a link to a *different* article.
    """
    h1 = None
    for sel in ("article h1", "main h1", "h1"):
        h1 = parser.css_first(sel)
        if h1 is not None:
            break
    if h1 is None:
        return None

    node: Node | None = h1
    for _ in range(5):
        node = node.parent if node is not None else None
        if node is None:
            break
        for img in node.css("img"):
            candidate = _img_best_candidate(img)
            if candidate is None:
                continue
            abs_url = urljoin(base_url, candidate[0])
            if _looks_like_non_lead(abs_url):
                continue
            if _links_to_other_article(_nearest_anchor(img), base_url):
                continue
            return abs_url
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
    # Check the path, not the raw URL, so a query string doesn't hide the
    # extension (e.g. ``gift-icon.svg?_dc=123``). SVGs are icons/logos, never
    # news lead photos.
    if urlparse(u).path.rstrip("/").endswith(".svg"):
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
