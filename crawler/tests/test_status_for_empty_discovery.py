"""Tests for the empty-discovery status mapper.

These pin down the distinction the user asked for: "no_articles_found"
should mean a clean fetch with no candidates in window, not a 4xx WAF
block or a parse failure dressed up the same way.
"""
from pmd_crawler.discovery import status_for_empty_discovery


def test_clean_empty_source_is_no_articles_found():
    # Source fetched, parsed, contained zero candidates in window.
    assert status_for_empty_discovery("config:rss", None) == "no_articles_found"


def test_http_error_becomes_discovery_blocked():
    # WAF / cloud-IP refusal — distinct from "we couldn't find articles".
    assert status_for_empty_discovery("config:sitemap", "http_error") == "discovery_blocked"


def test_parse_error_becomes_discovery_parse_error():
    # 200 but body wasn't valid XML/RSS — often a challenge page.
    assert status_for_empty_discovery("guess:sitemap", "parse_error") == "discovery_parse_error"


def test_network_error_collapses_to_unreachable():
    assert status_for_empty_discovery("config:rss", "network_error") == "unreachable"


def test_unreachable_strategy_wins_regardless_of_err():
    # If the upstream pipeline already concluded the site is unreachable,
    # don't downgrade the status with a stale per-attempt error.
    assert status_for_empty_discovery("unreachable", "http_error") == "unreachable"
    assert status_for_empty_discovery("unreachable", None) == "unreachable"
