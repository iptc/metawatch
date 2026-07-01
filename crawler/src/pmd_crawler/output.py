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
import pyarrow.compute as pc
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
    # Bot-management / access-blocking vendor identified at crawl time (e.g.
    # "cloudflare", "akamai", "datadome"). None when no positive fingerprint
    # was found. Only populated when status is blocked_by_waf.
    block_vendor: str | None = None


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
    # Per-page TDMRep declaration: 0/1 from <meta name="tdm-reservation">,
    # None when the tag is absent.
    tdm_reservation: int | None = None

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
    iptc_xmp_tags_json: str | None = None  # JSON-encoded filtered tag dict, see scoring.stored_evidence_tags (IPTC/XMP + scored EXIF)
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
    """Write a full crawl run to ``out_dir``, overwriting any existing files."""
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_table(out_dir / "runs.parquet", _runs_table([run_output.run]))
    _write_table(out_dir / "sites.parquet", _sites_table(run_output.sites))
    _write_table(out_dir / "articles.parquet", _articles_table(run_output.articles))
    _write_table(out_dir / "images.parquet", _images_table(run_output.images))
    _write_table(out_dir / "metadata_fields.parquet", _metadata_fields_table(run_output.metadata_fields))
    _write_table(out_dir / "robots_analysis.parquet", _robots_analysis_table(run_output.robots_analysis))


def merge_run(out_dir: Path, run_output: RunOutput) -> None:
    """Update an existing run directory with the sites in ``run_output``.

    Used for selective re-crawls (``pmd-crawler run --merge --site-id rnz``)
    so a partial run patches just the affected publishers without losing the
    rest of the day's data. Behaviour per table:

    * ``runs.parquet`` — left untouched. The merge is a fix-up of the
      original run, not a new run, so the timestamp / totals / run_id stay
      as they were. All incoming rows are re-stamped with the original
      ``run_id`` so they remain consistent with the run header.
    * ``sites.parquet`` / ``articles.parquet`` / ``images.parquet`` /
      ``robots_analysis.parquet`` — rows whose ``site_id`` is in the
      incoming set are dropped, then incoming rows are appended.
    * ``metadata_fields.parquet`` has no ``site_id``; we drop rows whose
      ``image_url_hash`` belonged to a now-superseded image on a merged
      site (those hashes change when the new crawl picks a different
      lead image), then append incoming rows.

    Falls back to ``write_run`` if ``runs.parquet`` doesn't exist yet —
    so calling ``merge_run`` on a fresh directory just behaves like the
    first write, and the workflow can pass ``--merge`` unconditionally
    without a guard.
    """
    runs_path = out_dir / "runs.parquet"
    if not runs_path.exists():
        write_run(out_dir, run_output)
        return
    existing_runs = pq.read_table(runs_path)
    if existing_runs.num_rows == 0:
        write_run(out_dir, run_output)
        return

    # Adopt the existing run's identity on every incoming row.
    existing_run_id = existing_runs.column("run_id")[0].as_py()
    for row in run_output.sites:
        row.run_id = existing_run_id
    for row in run_output.articles:
        row.run_id = existing_run_id
    for row in run_output.images:
        row.run_id = existing_run_id
    for row in run_output.metadata_fields:
        row.run_id = existing_run_id
    for row in run_output.robots_analysis:
        row.run_id = existing_run_id

    site_ids = pa.array(sorted({s.site_id for s in run_output.sites}))

    # Hashes of OLD images on merged sites — needed to filter
    # metadata_fields, which has no site_id column of its own.
    images_path = out_dir / "images.parquet"
    if images_path.exists():
        existing_images = pq.read_table(images_path)
        if existing_images.num_rows > 0:
            on_merged = pc.is_in(existing_images.column("site_id"), value_set=site_ids)
            old_image_hashes = (
                existing_images.filter(on_merged).column("image_url_hash").to_pylist()
            )
        else:
            # Empty existing table: pyarrow infers `null` column types for
            # zero-row writes, and `pc.is_in(null-col, string-array)` raises
            # ArrowTypeError. Nothing to drop anyway.
            old_image_hashes = []
    else:
        old_image_hashes = []

    _merge_by_column(
        out_dir / "sites.parquet", "site_id", site_ids, _sites_table(run_output.sites),
    )
    _merge_by_column(
        out_dir / "articles.parquet", "site_id", site_ids,
        _articles_table(run_output.articles),
    )
    _merge_by_column(
        out_dir / "images.parquet", "site_id", site_ids, _images_table(run_output.images),
    )
    _merge_by_column(
        out_dir / "robots_analysis.parquet", "site_id", site_ids,
        _robots_analysis_table(run_output.robots_analysis),
    )
    _merge_by_column(
        out_dir / "metadata_fields.parquet", "image_url_hash",
        pa.array(sorted(set(old_image_hashes))),
        _metadata_fields_table(run_output.metadata_fields),
    )
    # runs.parquet: deliberately untouched.


def _merge_by_column(
    path: Path, drop_column: str, drop_values: pa.Array, new_table: pa.Table,
) -> None:
    """Drop rows where ``row[drop_column] ∈ drop_values`` and append ``new_table``.

    If ``path`` already exists with non-zero rows, ``new_table`` is cast to
    that file's schema before the concat. Without this, a partial re-crawl
    where every site happened to have ``None`` in a given column produces
    a ``null``-typed column in ``new_table``, and ``pa.concat_tables``
    refuses to combine ``string`` (existing) with ``null`` (new). The cast
    direction ``null → typed`` always succeeds and preserves the existing
    parquet file's canonical schema.
    """
    if path.exists():
        existing = pq.read_table(path)
        if existing.num_rows > 0:
            new_table = new_table.cast(existing.schema)
            if len(drop_values) > 0:
                keep_mask = pc.invert(pc.is_in(existing.column(drop_column), value_set=drop_values))
                kept = existing.filter(keep_mask)
            else:
                kept = existing
        else:
            kept = existing
        merged = (
            pa.concat_tables([kept, new_table])
            if kept.num_rows > 0
            else new_table
        )
    else:
        merged = new_table
    _write_table(path, merged)


def _write_table(path: Path, table: pa.Table) -> None:
    pq.write_table(table, path, compression="zstd")


# ─── per-table column builders ──────────────────────────────────────────────


def _runs_table(rows: list[RunRow]) -> pa.Table:
    return pa.table({
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


def _sites_table(rows: list[SiteRow]) -> pa.Table:
    return pa.table({
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
        "block_vendor": [r.block_vendor for r in rows],
    })


def _articles_table(rows: list[ArticleRow]) -> pa.Table:
    return pa.table({
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
        "tdm_reservation": [r.tdm_reservation for r in rows],
    })


def _images_table(rows: list[ImageRow]) -> pa.Table:
    return pa.table({
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


def _metadata_fields_table(rows: list[MetadataFieldRow]) -> pa.Table:
    return pa.table({
        "run_id": [r.run_id for r in rows],
        "image_url_hash": [r.image_url_hash for r in rows],
        "family": [r.family for r in rows],
        "field_name": [r.field_name for r in rows],
        "has_value": [r.has_value for r in rows],
    })


def _robots_analysis_table(rows: list[RobotsAnalysisRow]) -> pa.Table:
    return pa.table({
        "run_id": [r.run_id for r in rows],
        "site_id": [r.site_id for r in rows],
        "user_agent": [r.user_agent for r in rows],
        "operator": [r.operator for r in rows],
        "status": [r.status for r in rows],
    })
