"""Parquet writers for crawl results.

Schemas mirror SPEC.md §7. Phase 1 writes:
  runs, sites, articles, images, metadata_fields.
Phase 3 adds:
  robots_analysis (one row per (site, tracked-AI-UA)), plus AI-opt-out
  presence columns on SiteRow (tdmrep, ai.txt, RSL, trust.txt) and
  image-level signal columns on ImageRow (noai/noimageai, CAWG).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


@dataclass
class RunRow:
    run_id: str
    started_at: datetime
    ended_at: datetime
    crawler_version: str
    site_count_attempted: int
    site_count_succeeded: int
    site_count_robots_blocked: int
    article_count: int
    image_count: int


@dataclass
class SiteRow:
    run_id: str
    site_id: str
    site_name: str
    country: str
    category: str
    status: str
    robots_url: str
    sitemap_url_used: str | None
    discovery_strategy: str
    articles_sampled: int
    images_analysed: int
    mean_iptc_score: float
    # AI opt-out site-wide signal flags (Phase 3). All default to False/None
    # so existing call sites that don't yet populate them stay valid; the
    # crawler always sets them when the optout probes run.
    has_tdmrep: bool = False
    has_ai_txt: bool = False
    has_rsl: bool = False
    rsl_license_urls: list[str] = field(default_factory=list)
    has_trust_txt: bool = False
    trust_txt_datatraining: str | None = None  # e.g. "no", "yes" — None when directive absent
    ai_bots_blocked_count: int = 0  # how many tracked UAs the robots.txt disallows at root
    # Cloudflare Content Signals (contentsignals.org) — three known signals
    # are split into their own columns for easy querying. None means the
    # directive was not present in any Content-Signal: line in robots.txt.
    has_content_signals: bool = False
    content_signal_ai_train: str | None = None  # "no" / "yes" / None
    content_signal_ai_input: str | None = None
    content_signal_search: str | None = None


@dataclass
class ArticleRow:
    run_id: str
    site_id: str
    article_url: str
    publication_date: datetime | None
    title: str | None
    language: str | None
    keywords: list[str]
    jsonld_news_article: str | None
    http_status: int
    fetched_at: datetime

    @property
    def article_url_hash(self) -> str:
        return _sha1(self.article_url)


@dataclass
class ImageRow:
    run_id: str
    site_id: str
    article_url_hash: str
    image_url: str
    mime_type: str | None
    width: int | None
    height: int | None
    file_size_bytes: int | None
    http_status: int
    has_exif: bool
    has_iptc_iim: bool
    has_iptc_xmp: bool
    has_c2pa: bool
    c2pa_manifest_signer: str | None
    c2pa_validation_status: str | None
    cdn_provider: str
    cdn_optimizer_active: str
    metadata_field_count: int
    iptc_score: float
    # Defaults kept last so dataclass field-ordering rules are happy.
    c2pa_failure_codes: list[str] = field(default_factory=list)
    dst_iptc: str | None = None
    dst_c2pa: list[str] = field(default_factory=list)
    iptc_xmp_tags_json: str | None = None  # JSON-encoded filtered tag dict, see scoring.iptc_xmp_subset
    # AI opt-out image-level signals (Phase 3). `noai_tokens` collects
    # noai/noimageai/noml seen in either the image's X-Robots-Tag header or
    # the host article's <meta name="robots">. `cawg_training_mining_json`
    # is the JSON-serialised assertion data dict when present.
    noai_tokens: list[str] = field(default_factory=list)
    cawg_training_mining_json: str | None = None

    @property
    def image_url_hash(self) -> str:
        return _sha1(self.image_url)


@dataclass
class RobotsAnalysisRow:
    """One row per (site, tracked AI UA). See SPEC.md §7."""
    run_id: str
    site_id: str
    user_agent: str
    operator: str
    status: str  # "allowed" | "disallowed"


@dataclass
class MetadataFieldRow:
    run_id: str
    image_url_hash: str
    family: str  # EXIF | IPTC | XMP | C2PA
    field_name: str
    has_value: bool


@dataclass
class RunOutput:
    run: RunRow
    sites: list[SiteRow] = field(default_factory=list)
    articles: list[ArticleRow] = field(default_factory=list)
    images: list[ImageRow] = field(default_factory=list)
    metadata_fields: list[MetadataFieldRow] = field(default_factory=list)
    robots_analysis: list[RobotsAnalysisRow] = field(default_factory=list)


def write_run(out_dir: Path, run_output: RunOutput) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_runs(out_dir / "runs.parquet", [run_output.run])
    _write_sites(out_dir / "sites.parquet", run_output.sites)
    _write_articles(out_dir / "articles.parquet", run_output.articles)
    _write_images(out_dir / "images.parquet", run_output.images)
    _write_metadata_fields(out_dir / "metadata_fields.parquet", run_output.metadata_fields)
    _write_robots_analysis(out_dir / "robots_analysis.parquet", run_output.robots_analysis)


def _write_runs(path: Path, rows: list[RunRow]) -> None:
    table = pa.table({
        "run_id": [r.run_id for r in rows],
        "started_at": [r.started_at for r in rows],
        "ended_at": [r.ended_at for r in rows],
        "crawler_version": [r.crawler_version for r in rows],
        "site_count_attempted": [r.site_count_attempted for r in rows],
        "site_count_succeeded": [r.site_count_succeeded for r in rows],
        "site_count_robots_blocked": [r.site_count_robots_blocked for r in rows],
        "article_count": [r.article_count for r in rows],
        "image_count": [r.image_count for r in rows],
    })
    pq.write_table(table, path, compression="zstd")


def _write_sites(path: Path, rows: list[SiteRow]) -> None:
    table = pa.table({
        "run_id": [r.run_id for r in rows],
        "site_id": [r.site_id for r in rows],
        "site_name": [r.site_name for r in rows],
        "country": [r.country for r in rows],
        "category": [r.category for r in rows],
        "status": [r.status for r in rows],
        "robots_url": [r.robots_url for r in rows],
        "sitemap_url_used": [r.sitemap_url_used for r in rows],
        "discovery_strategy": [r.discovery_strategy for r in rows],
        "articles_sampled": [r.articles_sampled for r in rows],
        "images_analysed": [r.images_analysed for r in rows],
        "mean_iptc_score": [r.mean_iptc_score for r in rows],
        "has_tdmrep": [r.has_tdmrep for r in rows],
        "has_ai_txt": [r.has_ai_txt for r in rows],
        "has_rsl": [r.has_rsl for r in rows],
        "rsl_license_urls": [r.rsl_license_urls for r in rows],
        "has_trust_txt": [r.has_trust_txt for r in rows],
        "trust_txt_datatraining": [r.trust_txt_datatraining for r in rows],
        "ai_bots_blocked_count": [r.ai_bots_blocked_count for r in rows],
        "has_content_signals": [r.has_content_signals for r in rows],
        "content_signal_ai_train": [r.content_signal_ai_train for r in rows],
        "content_signal_ai_input": [r.content_signal_ai_input for r in rows],
        "content_signal_search": [r.content_signal_search for r in rows],
    })
    pq.write_table(table, path, compression="zstd")


def _write_articles(path: Path, rows: list[ArticleRow]) -> None:
    table = pa.table({
        "run_id": [r.run_id for r in rows],
        "site_id": [r.site_id for r in rows],
        "article_url": [r.article_url for r in rows],
        "article_url_hash": [r.article_url_hash for r in rows],
        "publication_date": [r.publication_date for r in rows],
        "title": [r.title for r in rows],
        "language": [r.language for r in rows],
        "keywords": [r.keywords for r in rows],
        "jsonld_news_article": [r.jsonld_news_article for r in rows],
        "http_status": [r.http_status for r in rows],
        "fetched_at": [r.fetched_at for r in rows],
    })
    pq.write_table(table, path, compression="zstd")


def _write_images(path: Path, rows: list[ImageRow]) -> None:
    table = pa.table({
        "run_id": [r.run_id for r in rows],
        "site_id": [r.site_id for r in rows],
        "article_url_hash": [r.article_url_hash for r in rows],
        "image_url": [r.image_url for r in rows],
        "image_url_hash": [r.image_url_hash for r in rows],
        "mime_type": [r.mime_type for r in rows],
        "width": [r.width for r in rows],
        "height": [r.height for r in rows],
        "file_size_bytes": [r.file_size_bytes for r in rows],
        "http_status": [r.http_status for r in rows],
        "has_exif": [r.has_exif for r in rows],
        "has_iptc_iim": [r.has_iptc_iim for r in rows],
        "has_iptc_xmp": [r.has_iptc_xmp for r in rows],
        "has_c2pa": [r.has_c2pa for r in rows],
        "c2pa_manifest_signer": [r.c2pa_manifest_signer for r in rows],
        "c2pa_validation_status": [r.c2pa_validation_status for r in rows],
        "c2pa_failure_codes": [r.c2pa_failure_codes for r in rows],
        "dst_iptc": [r.dst_iptc for r in rows],
        "dst_c2pa": [r.dst_c2pa for r in rows],
        "iptc_xmp_tags_json": [r.iptc_xmp_tags_json for r in rows],
        "cdn_provider": [r.cdn_provider for r in rows],
        "cdn_optimizer_active": [r.cdn_optimizer_active for r in rows],
        "metadata_field_count": [r.metadata_field_count for r in rows],
        "iptc_score": [r.iptc_score for r in rows],
        "noai_tokens": [r.noai_tokens for r in rows],
        "cawg_training_mining_json": [r.cawg_training_mining_json for r in rows],
    })
    pq.write_table(table, path, compression="zstd")


def _write_metadata_fields(path: Path, rows: list[MetadataFieldRow]) -> None:
    table = pa.table({
        "run_id": [r.run_id for r in rows],
        "image_url_hash": [r.image_url_hash for r in rows],
        "family": [r.family for r in rows],
        "field_name": [r.field_name for r in rows],
        "has_value": [r.has_value for r in rows],
    })
    pq.write_table(table, path, compression="zstd")


def _write_robots_analysis(path: Path, rows: list[RobotsAnalysisRow]) -> None:
    table = pa.table({
        "run_id": [r.run_id for r in rows],
        "site_id": [r.site_id for r in rows],
        "user_agent": [r.user_agent for r in rows],
        "operator": [r.operator for r in rows],
        "status": [r.status for r in rows],
    })
    pq.write_table(table, path, compression="zstd")
