"""Tests for the AI opt-out probe module."""
from __future__ import annotations

from pmd_crawler.optout import (
    KNOWN_UAS,
    analyse_robots_for_ai,
    extract_cawg_training_mining,
    scan_robots_directives,
    site_root,
)


# ──────────────────────────────────────────────────────────────────────────────
# robots.txt AI-bot matrix
# ──────────────────────────────────────────────────────────────────────────────

def test_known_uas_loaded():
    # YAML is sourced from Appendix A of the IPTC Generative AI Opt-Out Best
    # Practice Recommendations v2.0 plus a few additions from SPEC.md §8.
    # The exact count drifts as new bots appear; assert a sensible floor.
    assert len(KNOWN_UAS) >= 60
    ua_names = {u.ua for u in KNOWN_UAS}
    # Spot-check a mix: OpenAI/Anthropic/Google trainers, Common Crawl,
    # plus a couple of less obvious entries straight from the PDF appendix.
    for required in (
        "GPTBot", "ClaudeBot", "Google-Extended", "CCBot", "PerplexityBot",
        "bingbot", "Bytespider", "Claude-SearchBot", "BLEXBot",
    ):
        assert required in ua_names


def test_robots_blocks_gpt_only():
    robots = """\
User-agent: GPTBot
Disallow: /

User-agent: *
Allow: /
"""
    out = analyse_robots_for_ai(robots, "https://example.com")
    statuses = {v.ua: v.status for v in out.per_ua}
    assert statuses["GPTBot"] == "disallowed"
    assert statuses["ClaudeBot"] == "allowed"
    assert statuses["Google-Extended"] == "allowed"


def test_robots_blocks_all_via_star_wildcard():
    # `User-agent: *` with `Disallow: /` blocks every tracked UA too, since
    # no UA-specific rule overrides it.
    robots = "User-agent: *\nDisallow: /\n"
    out = analyse_robots_for_ai(robots, "https://example.com")
    assert all(v.status == "disallowed" for v in out.per_ua)
    assert out.blocked_count == len(KNOWN_UAS)


def test_robots_empty_text_means_allowed():
    out = analyse_robots_for_ai("", "https://example.com")
    assert out.blocked_count == 0
    assert all(v.status == "allowed" for v in out.per_ua)


def test_robots_partial_path_does_not_count_as_disallowed():
    # GPTBot disallowed only from /admin — site root is still fetchable, so
    # we report "allowed" at this granularity.
    robots = "User-agent: GPTBot\nDisallow: /admin\n"
    out = analyse_robots_for_ai(robots, "https://example.com")
    statuses = {v.ua: v.status for v in out.per_ua}
    assert statuses["GPTBot"] == "allowed"


def test_rsl_license_directive_detected():
    robots = """\
User-agent: *
Allow: /
License: https://example.com/license.xml
"""
    out = analyse_robots_for_ai(robots, "https://example.com")
    assert out.has_rsl is True
    assert out.rsl_license_urls == ["https://example.com/license.xml"]


def test_rsl_license_case_insensitive_and_multiple():
    robots = """\
license: https://example.com/a.xml
License: https://example.com/b.xml
"""
    out = analyse_robots_for_ai(robots, "https://example.com")
    assert out.rsl_license_urls == ["https://example.com/a.xml", "https://example.com/b.xml"]


def test_rsl_absent_when_no_license_line():
    out = analyse_robots_for_ai("User-agent: *\nDisallow:\n", "https://example.com")
    assert out.has_rsl is False
    assert out.rsl_license_urls == []


# ──────────────────────────────────────────────────────────────────────────────
# Cloudflare Content Signals (contentsignals.org)
# ──────────────────────────────────────────────────────────────────────────────

def test_content_signals_single_line_three_signals():
    robots = """\
User-agent: *
Content-Signal: search=yes, ai-input=no, ai-train=no
"""
    out = analyse_robots_for_ai(robots, "https://example.com")
    assert out.has_content_signals is True
    assert out.content_signals == {"search": "yes", "ai-input": "no", "ai-train": "no"}


def test_content_signals_case_insensitive_directive():
    robots = "content-signal: ai-train=no\n"
    out = analyse_robots_for_ai(robots, "https://example.com")
    assert out.content_signals == {"ai-train": "no"}


def test_content_signals_multiple_lines_last_wins():
    # Later directives override earlier ones per the spec's stated convention.
    robots = """\
Content-Signal: ai-train=yes
Content-Signal: ai-train=no, search=yes
"""
    out = analyse_robots_for_ai(robots, "https://example.com")
    assert out.content_signals == {"ai-train": "no", "search": "yes"}


def test_content_signals_absent():
    out = analyse_robots_for_ai("User-agent: *\nDisallow:\n", "https://example.com")
    assert out.has_content_signals is False
    assert out.content_signals == {}


def test_content_signals_ignores_inline_comment():
    robots = "Content-Signal: ai-train=no  # please don't train on us\n"
    out = analyse_robots_for_ai(robots, "https://example.com")
    assert out.content_signals == {"ai-train": "no"}


def test_content_signals_malformed_pair_skipped():
    # A bare token without `=` is ignored, the rest still parses.
    robots = "Content-Signal: bogus, ai-train=no\n"
    out = analyse_robots_for_ai(robots, "https://example.com")
    assert out.content_signals == {"ai-train": "no"}


# ──────────────────────────────────────────────────────────────────────────────
# noai / noimageai token scanning
# ──────────────────────────────────────────────────────────────────────────────

def test_scan_robots_directives_finds_noai():
    assert scan_robots_directives("noai") == ["noai"]
    assert scan_robots_directives("noai, noimageai") == ["noai", "noimageai"]


def test_scan_robots_directives_finds_iptc_recommended_combo():
    # IPTC AI Opt-Out Best Practices v2.0 Rec 3 recommends this exact combo.
    tokens = scan_robots_directives("noarchive,nosnippet,noai,noimageai")
    assert set(tokens) == {"noarchive", "nosnippet", "noai", "noimageai"}


def test_scan_robots_directives_ignores_unrelated_tokens():
    # noindex/nofollow are not AI opt-outs in this sense.
    assert scan_robots_directives("noindex, nofollow") == []


def test_scan_robots_directives_handles_bot_specific_prefix():
    # X-Robots-Tag can carry per-bot rules: `googlebot: noai, noimageai`.
    assert scan_robots_directives("googlebot: noai, noimageai") == ["noai", "noimageai"]


def test_scan_robots_directives_dedupes():
    assert scan_robots_directives("noai, noai, noai") == ["noai"]


def test_scan_robots_directives_none_input():
    assert scan_robots_directives(None) == []
    assert scan_robots_directives("") == []


# ──────────────────────────────────────────────────────────────────────────────
# CAWG training-and-data-mining assertion extraction
# ──────────────────────────────────────────────────────────────────────────────

def test_cawg_assertion_present():
    manifest = {
        "assertions": [
            {"label": "c2pa.actions.v2", "data": {"actions": []}},
            {
                "label": "cawg.training-mining",
                "data": {"entries": {"cawg.ai_generative_training": {"use": "notAllowed"}}},
            },
        ]
    }
    result = extract_cawg_training_mining(manifest)
    assert result is not None
    assert result["entries"]["cawg.ai_generative_training"]["use"] == "notAllowed"


def test_cawg_assertion_legacy_label():
    manifest = {
        "assertions": [
            {"label": "cawg.training-and-data-mining.v1", "data": {"entries": {}}},
        ]
    }
    result = extract_cawg_training_mining(manifest)
    assert result == {"entries": {}}


def test_cawg_assertion_absent():
    manifest = {"assertions": [{"label": "c2pa.actions.v2", "data": {}}]}
    assert extract_cawg_training_mining(manifest) is None


def test_cawg_assertion_empty_manifest():
    assert extract_cawg_training_mining(None) is None
    assert extract_cawg_training_mining({}) is None
    assert extract_cawg_training_mining({"assertions": []}) is None


# ──────────────────────────────────────────────────────────────────────────────
# site_root — host-level URL construction for well-known probes
# ──────────────────────────────────────────────────────────────────────────────

def test_site_root_strips_path():
    # The bug this guards against: a configured RSS-feed URL like
    # https://news.yahoo.com/rss/ would otherwise lead to
    # https://news.yahoo.com/rss/.well-known/tdmrep.json, which on permissive
    # hosts (Yahoo serves any /rss/* as the RSS feed) returns a misleading 200.
    assert site_root("https://news.yahoo.com/rss/") == "https://news.yahoo.com/"


def test_site_root_handles_no_trailing_slash():
    assert site_root("https://example.com") == "https://example.com/"


def test_site_root_strips_query_and_fragment():
    assert site_root("https://example.com/path?q=1#frag") == "https://example.com/"


def test_site_root_preserves_port():
    assert site_root("https://example.com:8443/x/") == "https://example.com:8443/"
