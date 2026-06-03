"""Lead-image extraction tests.

Covers the seven-step priority chain and the filter rules. Each finder is
exercised in isolation, then the full chain to verify ordering and fallthrough.
"""
import json

from selectolax.parser import HTMLParser

from pmd_crawler.articles import (
    MIN_DIM,
    _extract_main_image,
    _extract_tdm_reservation,
    _from_dom_walk,
    _from_itemprop_image,
    _from_jsonld,
    _from_link_image_src,
    _from_og_image,
    _from_twitter_image,
    _images_from_jsonld,
    _img_best_candidate,
    _from_headline_adjacent,
    _looks_like_non_lead,
    _looks_like_placeholder,
)


def _doc(body: str) -> HTMLParser:
    return HTMLParser(f"<!doctype html><html><head></head><body>{body}</body></html>")


# ─── JSON-LD ─────────────────────────────────────────────────────────────────


def test_jsonld_type_as_list_does_not_raise():
    # Regression: @type often comes as a list like ["Article", "NewsArticle"].
    raw = json.dumps({"@type": ["Article", "NewsArticle"], "image": "https://x/y.jpg"})
    assert _images_from_jsonld(raw) == ["https://x/y.jpg"]


def test_jsonld_type_as_string_still_works():
    raw = json.dumps({"@type": "NewsArticle", "image": "https://x/y.jpg"})
    assert _images_from_jsonld(raw) == ["https://x/y.jpg"]


def test_jsonld_unknown_type_returns_no_images():
    raw = json.dumps({"@type": "WebPage", "image": "https://x/y.jpg"})
    assert _images_from_jsonld(raw) == []


def test_jsonld_news_article_subtypes_are_recognised():
    # schema.org NewsArticle subtypes carry the same lead-image semantics;
    # anything ending in "NewsArticle" should be accepted.
    for subtype in (
        "AnalysisNewsArticle",
        "AskPublicNewsArticle",
        "BackgroundNewsArticle",
        "OpinionNewsArticle",
        "ReportageNewsArticle",
        "ReviewNewsArticle",
    ):
        raw = json.dumps({"@type": subtype, "image": "https://x/y.jpg"})
        assert _images_from_jsonld(raw) == ["https://x/y.jpg"], subtype


def test_jsonld_subtype_in_type_list_is_recognised():
    raw = json.dumps({"@type": ["OpinionNewsArticle", "Article"], "image": "https://x/y.jpg"})
    assert _images_from_jsonld(raw) == ["https://x/y.jpg"]


def test_jsonld_type_merely_containing_article_is_not_matched():
    # Guard the suffix rule: "SocialMediaPosting" or a bogus "NewsArticleList"
    # should not slip through. We only accept exact Article or *NewsArticle.
    raw = json.dumps({"@type": "NewsArticleList", "image": "https://x/y.jpg"})
    assert _images_from_jsonld(raw) == []


def test_jsonld_image_as_object():
    raw = json.dumps({"@type": "NewsArticle", "image": {"url": "https://x/y.jpg"}})
    assert _images_from_jsonld(raw) == ["https://x/y.jpg"]


def test_from_jsonld_picks_news_article():
    html = _doc(
        '<script type="application/ld+json">'
        + json.dumps({"@type": "NewsArticle", "image": "https://x/hero.jpg"})
        + "</script>"
    )
    assert _from_jsonld(html) == "https://x/hero.jpg"


# ─── og:image / twitter:image / link rel=image_src ──────────────────────────


def test_from_og_image_basic():
    html = _doc('<meta property="og:image" content="https://x/og.jpg">')
    assert _from_og_image(html) == "https://x/og.jpg"


def test_from_og_image_url_variant():
    html = _doc('<meta property="og:image:url" content="https://x/og.jpg">')
    assert _from_og_image(html) == "https://x/og.jpg"


def test_from_og_image_empty_returns_none():
    html = _doc('<meta property="og:image" content="">')
    assert _from_og_image(html) is None


def test_from_twitter_image_basic():
    html = _doc('<meta name="twitter:image" content="https://x/tw.jpg">')
    assert _from_twitter_image(html) == "https://x/tw.jpg"


def test_from_twitter_image_src_variant():
    html = _doc('<meta name="twitter:image:src" content="https://x/tw.jpg">')
    assert _from_twitter_image(html) == "https://x/tw.jpg"


def test_from_link_image_src():
    html = _doc('<link rel="image_src" href="https://x/legacy.jpg">')
    assert _from_link_image_src(html) == "https://x/legacy.jpg"


# ─── itemprop="image" ────────────────────────────────────────────────────────


def test_itemprop_image_meta_form():
    html = _doc('<meta itemprop="image" content="https://x/mi.jpg">')
    assert _from_itemprop_image(html) == "https://x/mi.jpg"


def test_itemprop_image_img_form():
    html = _doc('<img itemprop="image" src="https://x/i.jpg">')
    assert _from_itemprop_image(html) == "https://x/i.jpg"


def test_itemprop_image_wraps_img():
    html = _doc(
        '<div itemprop="image"><img src="https://x/wrapped.jpg"></div>'
    )
    assert _from_itemprop_image(html) == "https://x/wrapped.jpg"


# ─── DOM walk (largest <img> in main content) ───────────────────────────────


def test_dom_walk_picks_largest_from_srcset_in_figure():
    html = _doc(
        '<article><figure><img srcset="https://x/small.jpg 400w, https://x/big.jpg 1600w, https://x/med.jpg 800w" src="https://x/small.jpg"></figure></article>'
    )
    assert _from_dom_walk(html, "https://news.example/") == "https://x/big.jpg"


def test_dom_walk_handles_picture_source():
    html = _doc(
        "<article><picture>"
        '<source srcset="https://x/large.jpg 1200w">'
        '<source srcset="https://x/small.jpg 400w">'
        '<img src="https://x/fallback.jpg">'
        "</picture></article>"
    )
    assert _from_dom_walk(html, "https://news.example/") == "https://x/large.jpg"


def test_dom_walk_handles_lazy_data_src():
    # src is a 1×1 placeholder; the real URL is on data-src.
    html = _doc(
        '<main><img src="data:image/gif;base64,R0lGOD" data-src="https://x/real.jpg" width="800"></main>'
    )
    assert _from_dom_walk(html, "https://news.example/") == "https://x/real.jpg"


def test_dom_walk_ignores_logos_and_avatars():
    html = _doc(
        "<article>"
        '<img src="https://cdn.x/avatar/u123.jpg" srcset="https://cdn.x/avatar/u123.jpg 600w">'
        '<figure><img srcset="https://cdn.x/hero.jpg 1200w"></figure>'
        '<img src="https://cdn.x/logo.png" srcset="https://cdn.x/logo.png 800w">'
        "</article>"
    )
    assert _from_dom_walk(html, "https://news.example/") == "https://cdn.x/hero.jpg"


def test_dom_walk_skips_below_min_dim():
    # Both candidates have widths but only one passes MIN_DIM.
    html = _doc(
        '<article><figure><img srcset="https://x/tiny.jpg 100w, https://x/lead.jpg 800w"></figure></article>'
    )
    assert MIN_DIM == 200
    assert _from_dom_walk(html, "https://news.example/") == "https://x/lead.jpg"


def test_dom_walk_skips_ad_network_domains():
    html = _doc(
        "<article>"
        '<img srcset="https://ads.doubleclick.net/x/promo.jpg 1200w">'
        '<figure><img srcset="https://cdn.example/news.jpg 1200w"></figure>'
        "</article>"
    )
    assert _from_dom_walk(html, "https://news.example/") == "https://cdn.example/news.jpg"


def test_dom_walk_resolves_relative_urls():
    html = _doc(
        '<article><figure><img srcset="/img/hero.jpg 1200w"></figure></article>'
    )
    assert _from_dom_walk(html, "https://news.example/story-1") == "https://news.example/img/hero.jpg"


def test_dom_walk_returns_none_when_nothing_qualifies():
    html = _doc("<p>no images</p>")
    assert _from_dom_walk(html, "https://news.example/") is None


# ─── Priority chain ─────────────────────────────────────────────────────────


def test_chain_prefers_jsonld_over_og():
    html = _doc(
        '<script type="application/ld+json">'
        + json.dumps({"@type": "NewsArticle", "image": "https://x/jsonld.jpg"})
        + "</script>"
        '<meta property="og:image" content="https://x/og.jpg">'
        '<meta name="twitter:image" content="https://x/tw.jpg">'
    )
    assert _extract_main_image(html, "https://news.example/") == "https://x/jsonld.jpg"


def test_chain_falls_through_to_twitter():
    html = _doc(
        '<meta name="twitter:image" content="https://x/tw.jpg">'
    )
    assert _extract_main_image(html, "https://news.example/") == "https://x/tw.jpg"


def test_chain_falls_through_to_link_image_src():
    html = _doc(
        '<link rel="image_src" href="https://x/legacy.jpg">'
    )
    assert _extract_main_image(html, "https://news.example/") == "https://x/legacy.jpg"


def test_chain_uses_sitemap_image_when_no_html_metadata():
    html = _doc("<p>article body, no metadata declarations</p>")
    assert _extract_main_image(html, "https://news.example/", sitemap_image="https://x/sitemap.jpg") == "https://x/sitemap.jpg"


def test_chain_html_metadata_beats_sitemap_image():
    # JSON-LD/og are authoritative — sitemap is a fallback, not an override.
    html = _doc('<meta property="og:image" content="https://x/og.jpg">')
    assert _extract_main_image(html, "https://news.example/", sitemap_image="https://x/sitemap.jpg") == "https://x/og.jpg"


def test_chain_falls_through_to_dom_walk_when_no_meta_and_no_sitemap():
    html = _doc(
        '<article><figure><img srcset="https://x/hero.jpg 1200w"></figure></article>'
    )
    assert _extract_main_image(html, "https://news.example/") == "https://x/hero.jpg"


def test_chain_returns_none_when_nothing_found():
    html = _doc("<p>nothing here</p>")
    assert _extract_main_image(html, "https://news.example/") is None


def test_chain_resolves_relative_og_url():
    html = _doc('<meta property="og:image" content="/local/hero.jpg">')
    assert _extract_main_image(html, "https://news.example/story-1") == "https://news.example/local/hero.jpg"


# ─── Filter helpers ─────────────────────────────────────────────────────────


def test_placeholder_detection():
    assert _looks_like_placeholder("data:image/svg+xml;base64,...")
    assert _looks_like_placeholder("data:image/gif;base64,R0lG")
    assert _looks_like_placeholder("")
    assert _looks_like_placeholder("https://x/spinner.svg")
    assert not _looks_like_placeholder("https://x/hero.jpg")


def test_non_lead_url_patterns():
    assert _looks_like_non_lead("https://cdn.example/users/avatars/u123.jpg")
    assert _looks_like_non_lead("https://cdn.example/sprite-v3.png")
    assert _looks_like_non_lead("https://cdn.example/logo-2x.png")
    assert _looks_like_non_lead("https://ads.doubleclick.net/banner.png")
    assert _looks_like_non_lead("https://cdn.taboola.com/promo.jpg")
    assert not _looks_like_non_lead("https://cdn.example/news/2026/05/lead.jpg")


# ─── Candidate width parsing ────────────────────────────────────────────────


def test_img_best_candidate_picks_widest_w_descriptor():
    html = _doc(
        '<img srcset="https://x/a.jpg 400w, https://x/b.jpg 800w, https://x/c.jpg 1200w" src="https://x/fallback.jpg">'
    )
    img = html.css_first("img")
    assert _img_best_candidate(img) == ("https://x/c.jpg", 1200)


def test_img_best_candidate_density_descriptor_falls_back_to_zero_width():
    # 2x descriptors give us no width info; still returns the URL.
    html = _doc('<img srcset="https://x/a.jpg 1x, https://x/b.jpg 2x">')
    img = html.css_first("img")
    candidate = _img_best_candidate(img)
    assert candidate is not None
    url, width = candidate
    assert url in ("https://x/a.jpg", "https://x/b.jpg")
    assert width == 0


def test_img_best_candidate_no_srcset_uses_real_src_and_width_attr():
    html = _doc('<img src="https://x/plain.jpg" width="800">')
    img = html.css_first("img")
    assert _img_best_candidate(img) == ("https://x/plain.jpg", 800)


def test_img_best_candidate_uses_data_src_when_src_is_placeholder():
    html = _doc(
        '<img src="data:image/gif;base64,R0lGOD" data-src="https://x/real.jpg" width="800">'
    )
    img = html.css_first("img")
    assert _img_best_candidate(img) == ("https://x/real.jpg", 800)


# ─── TDMRep per-page meta tag ───────────────────────────────────────────────


def test_tdm_reservation_value_1():
    html = HTMLParser('<head><meta name="tdm-reservation" content="1"></head>')
    assert _extract_tdm_reservation(html) == 1


def test_tdm_reservation_value_0():
    html = HTMLParser('<head><meta name="tdm-reservation" content="0"></head>')
    assert _extract_tdm_reservation(html) == 0


def test_tdm_reservation_absent_returns_none():
    html = HTMLParser('<head><meta name="robots" content="noai"></head>')
    assert _extract_tdm_reservation(html) is None


def test_tdm_reservation_tolerates_whitespace_and_quotes():
    html = HTMLParser('<head><meta name="tdm-reservation" content=" 1 "></head>')
    assert _extract_tdm_reservation(html) == 1


def test_tdm_reservation_invalid_value_returns_none():
    # Spec only defines 0 and 1; anything else we treat as absent.
    html = HTMLParser('<head><meta name="tdm-reservation" content="reserved"></head>')
    assert _extract_tdm_reservation(html) is None


def test_tdm_reservation_uppercase_name_accepted():
    html = HTMLParser('<head><meta name="TDM-Reservation" content="1"></head>')
    assert _extract_tdm_reservation(html) == 1


# ─── Headline-adjacent fallback ──────────────────────────────────────────────

BASE = "https://news.example/story-one/"


def test_headline_adjacent_recovers_lead_next_to_h1():
    # Non-semantic div layout, no og:image — the lead photo sits in the same
    # column as the <h1> and is not a cross-link. (The Baltic Times pattern.)
    html = _doc(
        '<div class="col"><h1>Big story</h1>'
        '<div class="lead"><a href="#"><img src="https://cdn.example/photos/123_big.jpg"></a></div>'
        '</div>'
    )
    assert _from_headline_adjacent(html, BASE) == "https://cdn.example/photos/123_big.jpg"


def test_headline_adjacent_skips_cross_linked_thumbnails():
    # Related-article thumbnails live in the same column but link elsewhere.
    html = _doc(
        '<div class="col"><h1>Brief with no photo</h1>'
        '<div class="related">'
        '<a href="/other-story/"><img src="https://cdn.example/photos/999_big.jpg"></a>'
        '<a href="/third-story/"><img src="https://cdn.example/photos/888_big.jpg"></a>'
        '</div></div>'
    )
    assert _from_headline_adjacent(html, BASE) is None


def test_headline_adjacent_allows_self_link():
    html = _doc(
        '<div><h1>Self linked lead</h1>'
        '<a href="/story-one/"><img src="https://cdn.example/photos/55_big.jpg"></a></div>'
    )
    assert _from_headline_adjacent(html, BASE) == "https://cdn.example/photos/55_big.jpg"


def test_headline_adjacent_skips_svg_icon_with_query():
    # Regression: gift-icon.svg?_dc=123 must not be picked as a lead.
    html = _doc(
        '<div><h1>Story</h1>'
        '<img src="https://cdn.example/images/gift-icon.svg?_dc=1778693814">'
        '<a href="#"><img src="https://cdn.example/photos/77_big.jpg"></a></div>'
    )
    assert _from_headline_adjacent(html, BASE) == "https://cdn.example/photos/77_big.jpg"


def test_headline_adjacent_none_without_h1():
    html = _doc('<div><img src="https://cdn.example/photos/1_big.jpg"></div>')
    assert _from_headline_adjacent(html, BASE) is None


def test_og_image_still_wins_over_headline_adjacent():
    html = HTMLParser(
        '<!doctype html><html><head>'
        '<meta property="og:image" content="https://cdn.example/og/lead.jpg">'
        '</head><body><div><h1>Story</h1>'
        '<a href="#"><img src="https://cdn.example/photos/99_big.jpg"></a>'
        '</div></body></html>'
    )
    assert _extract_main_image(html, BASE) == "https://cdn.example/og/lead.jpg"


def test_placeholder_svg_detection_ignores_query_string():
    assert _looks_like_placeholder("https://x/gift-icon.svg?_dc=123") is True
    assert _looks_like_placeholder("https://x/photos/real_big.jpg?w=600") is False
