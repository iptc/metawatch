"""Article fetch and image-URL extraction."""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
from selectolax.parser import HTMLParser

from .discovery import ArticleCandidate

MIN_DIM = 200


@dataclass
class ArticleResult:
    candidate: ArticleCandidate
    http_status: int
    image_urls: list[str]
    jsonld_news_article: str | None  # raw JSON string, may be empty


async def fetch_article(
    client: httpx.AsyncClient, candidate: ArticleCandidate
) -> ArticleResult:
    # The sitemap may have advertised the main image directly. Trust the first one.
    if candidate.image_urls_from_sitemap:
        return ArticleResult(
            candidate=candidate,
            http_status=0,
            image_urls=[candidate.image_urls_from_sitemap[0]],
            jsonld_news_article=None,
        )

    try:
        r = await client.get(candidate.url, timeout=15.0, follow_redirects=True)
    except Exception:
        return ArticleResult(candidate, 0, [], None)

    if r.status_code >= 400 or not r.content:
        return ArticleResult(candidate, r.status_code, [], None)

    parser = HTMLParser(r.text)
    base_url = r.url.human_repr() if hasattr(r.url, "human_repr") else str(r.url)
    main_image = _extract_main_image(parser, base_url)
    jsonld = _extract_news_article_jsonld(parser)
    return ArticleResult(candidate, r.status_code, [main_image] if main_image else [], jsonld)


def _extract_main_image(parser: HTMLParser, base_url: str) -> str | None:
    """Return the single main image for an article, or None.

    Priority:
      1. JSON-LD NewsArticle/Article `image` field (most authoritative — publisher's own declaration).
      2. <meta property="og:image"> — Open Graph lead image, almost universally the hero.
    Body-DOM `<img>` tags are not used: they pick up site chrome, lazy-load placeholders,
    related-article thumbnails, and inline ads.
    """
    for node in parser.css('script[type="application/ld+json"]'):
        text = node.text() or ""
        for img in _images_from_jsonld(text):
            return urljoin(base_url, img)

    for og in parser.css('meta[property="og:image"], meta[property="og:image:url"]'):
        content = og.attributes.get("content")
        if content:
            return urljoin(base_url, content)

    return None


def _images_from_jsonld(raw: str) -> list[str]:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    out: list[str] = []
    _walk_jsonld_for_images(data, out)
    return out


def _walk_jsonld_for_images(node: object, out: list[str]) -> None:
    if isinstance(node, dict):
        t = node.get("@type")
        types = {t} if isinstance(t, str) else set(t) if isinstance(t, list) else set()
        if types & {"NewsArticle", "Article", "ImageObject"}:
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


def _extract_news_article_jsonld(parser: HTMLParser) -> str | None:
    for node in parser.css('script[type="application/ld+json"]'):
        text = node.text() or ""
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        if _has_type(data, {"NewsArticle", "Article"}):
            return text
    return None


def _has_type(node: object, types: set[str]) -> bool:
    if isinstance(node, dict):
        t = node.get("@type")
        if isinstance(t, str) and t in types:
            return True
        if isinstance(t, list) and any(x in types for x in t):
            return True
        for v in node.values():
            if _has_type(v, types):
                return True
    elif isinstance(node, list):
        for v in node:
            if _has_type(v, types):
                return True
    return False
