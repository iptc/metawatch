"""Tests for output.merge_run — partial-crawl merging into an existing run dir.

The contract under test:

  * write_run(out, A); merge_run(out, B)  where A's sites and B's sites overlap
    on a set S leaves the directory with: A's run row untouched; A's rows for
    sites NOT in S preserved; A's rows for sites in S replaced by B's; B's rows
    re-stamped with A's run_id.
  * merge_run on a fresh directory behaves like write_run.
  * metadata_fields.parquet uses image_url_hash filtering (no site_id column),
    so its rows are correctly dropped along with the images they belong to.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from pmd_crawler.output import (
    ArticleRow,
    ImageRow,
    MetadataFieldRow,
    RobotsAnalysisRow,
    RunOutput,
    RunRow,
    SiteRow,
    merge_run,
    write_run,
)


def _run(run_id: str) -> RunRow:
    now = datetime.now(UTC)
    return RunRow(
        run_id=run_id, started_at=now, ended_at=now, crawler_version="test",
        site_count_attempted=0, site_count_succeeded=0,
        site_count_robots_blocked=0, article_count=0, image_count=0,
    )


def _site(run_id: str, site_id: str, name: str = "", status: str = "ok") -> SiteRow:
    return SiteRow(
        run_id=run_id, site_id=site_id, site_name=name or site_id,
        country="zz", category="newspaper", status=status, robots_url="",
        sitemap_url_used=None, discovery_strategy="config:rss",
        articles_sampled=0, images_analysed=0, mean_iptc_score=0.0,
    )


def _article(run_id: str, site_id: str, url: str) -> ArticleRow:
    return ArticleRow(
        run_id=run_id, site_id=site_id, article_url=url,
        publication_date=None, title=None, language=None, keywords=[],
        jsonld_news_article=None, http_status=200, fetched_at=datetime.now(UTC),
    )


def _image(run_id: str, site_id: str, image_url: str, article_url: str) -> ImageRow:
    art_hash = ArticleRow(
        run_id="", site_id="", article_url=article_url, publication_date=None,
        title=None, language=None, keywords=[], jsonld_news_article=None,
        http_status=0, fetched_at=datetime.now(UTC),
    ).article_url_hash
    return ImageRow(
        run_id=run_id, site_id=site_id, article_url_hash=art_hash,
        image_url=image_url, mime_type=None, width=None, height=None,
        file_size_bytes=None, http_status=200,
        has_exif=False, has_iptc_iim=False, has_iptc_xmp=False, has_c2pa=False,
        c2pa_manifest_signer=None, c2pa_validation_status=None,
        cdn_provider="unknown", cdn_optimizer_active="unknown",
        metadata_field_count=0, iptc_score=0.0,
    )


def _field(run_id: str, image_url_hash: str, field_name: str) -> MetadataFieldRow:
    return MetadataFieldRow(
        run_id=run_id, image_url_hash=image_url_hash,
        family="IPTC", field_name=field_name, has_value=True,
    )


def _robot(run_id: str, site_id: str, ua: str = "GPTBot", status: str = "allowed") -> RobotsAnalysisRow:
    return RobotsAnalysisRow(
        run_id=run_id, site_id=site_id, user_agent=ua, operator="x", status=status,
    )


# ──────────────────────────────────────────────────────────────────────────────


def test_merge_run_on_fresh_directory_behaves_like_write_run(tmp_path: Path) -> None:
    output = RunOutput(
        run=_run("R1"),
        sites=[_site("R1", "alpha")],
        articles=[_article("R1", "alpha", "https://alpha/1")],
        images=[],
        metadata_fields=[],
        robots_analysis=[_robot("R1", "alpha")],
    )
    merge_run(tmp_path, output)
    runs = pq.read_table(tmp_path / "runs.parquet")
    assert runs.num_rows == 1
    assert runs.column("run_id")[0].as_py() == "R1"
    sites = pq.read_table(tmp_path / "sites.parquet")
    assert sites.column("site_id").to_pylist() == ["alpha"]


def test_merge_replaces_only_named_sites_and_preserves_others(tmp_path: Path) -> None:
    # First, a full run with three sites.
    write_run(tmp_path, RunOutput(
        run=_run("R1"),
        sites=[_site("R1", s) for s in ("alpha", "beta", "gamma")],
        articles=[
            _article("R1", "alpha", "https://alpha/1"),
            _article("R1", "beta",  "https://beta/1"),
            _article("R1", "gamma", "https://gamma/1"),
        ],
        images=[],
        metadata_fields=[],
        robots_analysis=[_robot("R1", s) for s in ("alpha", "beta", "gamma")],
    ))

    # Now merge a partial run that re-crawls only `beta` with new data.
    merge_run(tmp_path, RunOutput(
        run=_run("R2"),  # will be ignored
        sites=[_site("R2", "beta", name="Beta (re-crawl)")],
        articles=[
            _article("R2", "beta", "https://beta/2"),
            _article("R2", "beta", "https://beta/3"),
        ],
        images=[],
        metadata_fields=[],
        robots_analysis=[_robot("R2", "beta", ua="ClaudeBot", status="disallowed")],
    ))

    # runs row unchanged
    runs = pq.read_table(tmp_path / "runs.parquet")
    assert runs.num_rows == 1
    assert runs.column("run_id")[0].as_py() == "R1"

    # sites: alpha + gamma from R1, beta from R2 with re-stamped R1 run_id
    sites = pq.read_table(tmp_path / "sites.parquet").to_pylist()
    assert sorted(s["site_id"] for s in sites) == ["alpha", "beta", "gamma"]
    beta = next(s for s in sites if s["site_id"] == "beta")
    assert beta["site_name"] == "Beta (re-crawl)"
    assert beta["run_id"] == "R1", "incoming beta row should be re-stamped"

    # articles: alpha/1 and gamma/1 kept; beta/1 dropped; beta/2 + beta/3 added.
    articles = pq.read_table(tmp_path / "articles.parquet").to_pylist()
    urls = sorted(a["article_url"] for a in articles)
    assert urls == ["https://alpha/1", "https://beta/2", "https://beta/3", "https://gamma/1"]
    for a in articles:
        assert a["run_id"] == "R1"

    # robots_analysis: alpha + gamma's "allowed" GPTBot kept; beta's GPTBot
    # dropped and replaced with ClaudeBot disallowed.
    robots = pq.read_table(tmp_path / "robots_analysis.parquet").to_pylist()
    by_site = {(r["site_id"], r["user_agent"]): r for r in robots}
    assert ("alpha", "GPTBot") in by_site
    assert ("gamma", "GPTBot") in by_site
    assert ("beta", "GPTBot") not in by_site
    assert by_site[("beta", "ClaudeBot")]["status"] == "disallowed"


def test_merge_drops_metadata_fields_for_old_images_on_merged_sites(tmp_path: Path) -> None:
    # alpha has one image with field "Creator"; beta has one image with field
    # "Caption". After merging a fresh beta run that picks a DIFFERENT image
    # (new hash) carrying "Headline", the old beta Caption row must be gone.
    img_alpha = _image("R1", "alpha", "https://alpha/im-a.jpg", "https://alpha/1")
    img_beta_old = _image("R1", "beta", "https://beta/im-old.jpg", "https://beta/1")
    write_run(tmp_path, RunOutput(
        run=_run("R1"),
        sites=[_site("R1", "alpha"), _site("R1", "beta")],
        articles=[_article("R1", "alpha", "https://alpha/1"),
                  _article("R1", "beta",  "https://beta/1")],
        images=[img_alpha, img_beta_old],
        metadata_fields=[
            _field("R1", img_alpha.image_url_hash,    "Creator"),
            _field("R1", img_beta_old.image_url_hash, "Caption"),
        ],
        robots_analysis=[],
    ))

    img_beta_new = _image("R2", "beta", "https://beta/im-new.jpg", "https://beta/2")
    merge_run(tmp_path, RunOutput(
        run=_run("R2"),
        sites=[_site("R2", "beta")],
        articles=[_article("R2", "beta", "https://beta/2")],
        images=[img_beta_new],
        metadata_fields=[_field("R2", img_beta_new.image_url_hash, "Headline")],
        robots_analysis=[],
    ))

    fields = pq.read_table(tmp_path / "metadata_fields.parquet").to_pylist()
    by_hash = {f["image_url_hash"]: f for f in fields}
    # alpha's Creator survives
    assert by_hash[img_alpha.image_url_hash]["field_name"] == "Creator"
    # beta's OLD image hash gone
    assert img_beta_old.image_url_hash not in by_hash
    # beta's NEW image hash present, with R1 run_id (re-stamped)
    assert by_hash[img_beta_new.image_url_hash]["field_name"] == "Headline"
    assert by_hash[img_beta_new.image_url_hash]["run_id"] == "R1"


def test_merge_adds_new_site_not_in_original_run(tmp_path: Path) -> None:
    # Useful for testing a brand-new publisher locally before adding to a full
    # crawl. The merge should append, not error.
    write_run(tmp_path, RunOutput(
        run=_run("R1"),
        sites=[_site("R1", "alpha")],
        articles=[], images=[], metadata_fields=[], robots_analysis=[],
    ))
    merge_run(tmp_path, RunOutput(
        run=_run("R2"),
        sites=[_site("R2", "zeta")],
        articles=[], images=[], metadata_fields=[], robots_analysis=[],
    ))
    sites = pq.read_table(tmp_path / "sites.parquet").to_pylist()
    assert sorted(s["site_id"] for s in sites) == ["alpha", "zeta"]
    assert all(s["run_id"] == "R1" for s in sites)
