"""Export the latest crawl run (or a specific one) to JSON for the Astro site.

Output:
  site/src/data/latest/
    summary.json        # run metadata + global stats
    countries.json      # per-country aggregates
    sites.json          # per-site aggregates + drill-down
    fields.json         # global per-field presence
    cdn.json            # CDN distribution + optimizer-strip cross-tab
    history.json        # rolling snapshot across all runs (for time series)
    runs_index.json     # one entry per run with file manifest (for dataset page)
    c2pa.json           # C2PA presence breakdown, by signer / outcome / site
    dst.json            # DigitalSourceType breakdown, buckets + raw URIs
    scoring.json        # mirror of config/scoring.yaml for the methodology page
    countries_names.json # mirror of config/countries.yaml for display labels

Usage:
    python scripts/export_for_site.py [--runs-dir ../data/runs] [--out ../site/src/data]
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean

import pyarrow.parquet as pq
import yaml

from pmd_crawler.discovery import _clean_title
from pmd_crawler.dst import short_term as _dst_short_term
from pmd_crawler.scoring import ALL_FIELDS, SCORED_FIELDS, TOTAL_WEIGHT, TRACKED_FIELDS

DST_VOCAB_PATH = Path(__file__).resolve().parents[2] / "config" / "dst_vocab.yaml"
COUNTRIES_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "countries.yaml"
PUBLISHERS_DIR = Path(__file__).resolve().parents[2] / "config" / "publishers"


def load_publisher_urls() -> dict[str, str]:
    """Return site_id → homepage URL from config/publishers/*.yaml."""
    out: dict[str, str] = {}
    if not PUBLISHERS_DIR.exists():
        return out
    for p in sorted(PUBLISHERS_DIR.glob("*.yaml")):
        if p.name.startswith("_"):
            continue
        data = yaml.safe_load(p.read_text()) or {}
        for site in data.get("sites", []) or []:
            sid = site.get("id")
            if sid:
                out[sid] = site.get("homepage") or site.get("url") or ""
    return out


def load_country_names() -> dict[str, str]:
    """Return alpha-2 → display name from config/countries.yaml.

    Guards against the YAML 1.1 "Norway problem": unquoted ``NO`` parses as
    the boolean False, ``ON`` as True. Re-stringify any boolean keys before
    returning, and warn so a missing-quotes mistake doesn't ship silently.
    """
    import sys

    if not COUNTRIES_CONFIG_PATH.exists():
        return {}
    data = yaml.safe_load(COUNTRIES_CONFIG_PATH.read_text()) or {}
    raw = data.get("names") or {}
    fixed: dict[str, str] = {}
    for k, v in raw.items():
        if k is False:
            print("WARN: country code 'NO' came through as YAML False; please quote it.", file=sys.stderr)
            fixed["NO"] = str(v)
        elif k is True:
            print("WARN: country code 'ON' came through as YAML True; please quote it.", file=sys.stderr)
            fixed["ON"] = str(v)
        else:
            fixed[str(k)] = str(v)
    return fixed


def load_dst_buckets() -> dict[str, str]:
    if not DST_VOCAB_PATH.exists():
        return {}
    data = yaml.safe_load(DST_VOCAB_PATH.read_text()) or {}
    return dict(data.get("buckets") or {})


def bucket_for_dst(uri: str | None, vocab: dict[str, str]) -> str:
    """Map a DST URI (or None) to a Metawatch bucket label."""
    term = _dst_short_term(uri)
    if not term:
        return "Not declared"
    return vocab.get(term, "Other")


def latest_run_dir(runs_dir: Path) -> Path:
    dirs = sorted([p for p in runs_dir.iterdir() if p.is_dir()])
    if not dirs:
        raise SystemExit(f"No runs found in {runs_dir}")
    return dirs[-1]


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return pq.read_table(path).to_pylist()


def fmt_dt(v) -> str | None:
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def images_with_any_field(fields: list[dict]) -> set[str]:
    """Return the set of image_url_hashes that have at least one scored or
    tracked field present (has_value=True) in metadata_fields.parquet.

    This is the correct basis for 'pct_with_iptc': an image qualifies if any
    field from scoring.yaml (scored_fields OR tracked_fields) was found,
    regardless of which metadata container (IPTC-IIM, XMP, or EXIF/TIFF)
    held it.
    """
    return {r["image_url_hash"] for r in fields if r["has_value"]}


def block_info(
    s: dict, articles_by_site: dict[str, list[dict]],
) -> tuple[str, str | None, str | None]:
    """Return (effective_status, block_phase, block_vendor) for a site.

    Unifies the three historical blocked statuses into a single 'blocked'
    value, and computes which phase of the crawl was blocked plus the
    vendor when known.

    Phases:
      'discovery' — the feed/sitemap fetch itself failed (blocked_by_waf /
                    discovery_blocked from the stored status).
      'article'   — discovery succeeded but ≥80 % of article fetches
                    returned errors and zero images were harvested.

    Vendor: stored in sites.parquet as block_vendor for new crawls;
    null for existing data and for article-phase blocks (response headers
    not stored at that granularity).

    Module-level rather than nested in ``export_run`` so the headline counts
    and the per-site rows classify sites identically — see
    ``derived_run_counts``.
    """
    raw_status = s["status"]

    # Discovery-phase blocks stored by the crawler.
    if raw_status in ("blocked_by_waf", "discovery_blocked"):
        vendor = s.get("block_vendor")  # null for pre-schema-change data
        return "blocked", "discovery", vendor

    # Article-phase: only when discovery succeeded but we got no images.
    if raw_status == "ok":
        site_arts = articles_by_site.get(s["site_id"], [])
        if site_arts and s.get("images_analysed", 0) == 0:
            n_blocked = sum(
                1 for a in site_arts
                if (a.get("http_status") or 0) == 0 or (a.get("http_status") or 0) >= 400
            )
            if n_blocked / len(site_arts) >= 0.8:
                return "blocked", "article", None

    return raw_status, None, None


def derived_run_counts(
    sites: list[dict], articles: list[dict], images: list[dict],
) -> dict[str, int]:
    """Recount a run's headline totals from its own tables.

    ``runs.parquet`` records the totals the crawler saw when the run was first
    written, but ``merge_run`` deliberately leaves that header untouched when a
    partial re-crawl patches sites into an existing run — the merge is a fix-up
    of the run, not a new run. After a merge the header therefore disagrees with
    sites/articles/images, which do reflect reality.

    Publishing the header counts alongside percentages computed from the tables
    put two different denominators on the same page (the 2026-08-01 merge left
    the site claiming 6,680 images while ``images.parquet`` held 6,715). Deriving
    every published count from the tables keeps one denominator throughout, and
    is identical to the header for any run that was never merged into.

    ``site_count_succeeded`` counts *effective* status, not the raw one stored
    by the crawler. A site whose discovery worked but whose every article fetch
    was refused (paywall or bot wall — the FT, Bloomberg, the Economist and
    Libération all behave this way) is stored as "ok" with zero images, but
    block_info() reclassifies it as blocked for the per-site rows. Counting the
    raw status here made the homepage advertise 445 publishers crawled while the
    Sites page listed 434 — one definition, used in both places, avoids that.
    """
    articles_by_site: dict[str, list[dict]] = defaultdict(list)
    for a in articles:
        articles_by_site[a["site_id"]].append(a)
    effective = [block_info(s, articles_by_site)[0] for s in sites]
    return {
        "site_count_attempted": len(sites),
        "site_count_succeeded": sum(1 for st in effective if st == "ok"),
        "site_count_robots_blocked": sum(1 for st in effective if st == "robots_disallow"),
        "article_count": len(articles),
        "image_count": len(images),
    }


def export_run(run_dir: Path, out_dir: Path, all_runs: list[Path]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    runs = read(run_dir / "runs.parquet")
    sites = read(run_dir / "sites.parquet")
    articles = read(run_dir / "articles.parquet")
    images = read(run_dir / "images.parquet")
    fields = read(run_dir / "metadata_fields.parquet")

    summary = {
        "run_id": runs[0]["run_id"] if runs else None,
        "started_at": fmt_dt(runs[0]["started_at"]) if runs else None,
        "ended_at": fmt_dt(runs[0]["ended_at"]) if runs else None,
        **derived_run_counts(sites, articles, images),
    }
    field_hashes = images_with_any_field(fields)
    image_scores = [img["iptc_score"] for img in images if img["http_status"] == 200]
    summary["global_mean_score"] = round(mean(image_scores), 2) if image_scores else 0.0
    summary["images_with_iptc"] = sum(1 for img in images if img["image_url_hash"] in field_hashes)
    summary["images_with_c2pa"] = sum(1 for img in images if img["has_c2pa"])
    summary["pct_with_iptc"] = (
        round(100.0 * summary["images_with_iptc"] / len(images), 1) if images else 0.0
    )
    summary["pct_with_c2pa"] = (
        round(100.0 * summary["images_with_c2pa"] / len(images), 2) if images else 0.0
    )

    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    # Mirror config/countries.yaml so the site can render country labels from
    # the same source the crawler config uses.
    (out_dir / "countries_names.json").write_text(
        json.dumps(load_country_names(), indent=2)
    )

    # Mirror config/scoring.yaml into JSON so the static site can render the
    # methodology table from the same source of truth the crawler uses.
    scoring_out = {
        "scored_fields": [
            {"label": label, "aliases": list(aliases), "weight": weight}
            for label, aliases, weight in SCORED_FIELDS
        ],
        "tracked_fields": [
            {"label": label, "aliases": list(aliases)}
            for label, aliases in TRACKED_FIELDS
        ],
        "total_weight": TOTAL_WEIGHT,
    }
    (out_dir / "scoring.json").write_text(json.dumps(scoring_out, indent=2))

    countries_agg: dict[str, dict] = defaultdict(lambda: {"sites": [], "scores": []})
    for s in sites:
        cc = s["country"]
        countries_agg[cc]["sites"].append(s["site_id"])
        if s["status"] == "ok":
            countries_agg[cc]["scores"].append(s["mean_iptc_score"])
    countries_out = [
        {
            "country": cc,
            "site_count": len(data["sites"]),
            "mean_score": round(mean(data["scores"]), 2) if data["scores"] else 0.0,
        }
        for cc, data in sorted(countries_agg.items())
    ]
    countries_out.sort(key=lambda x: x["mean_score"], reverse=True)
    (out_dir / "countries.json").write_text(json.dumps(countries_out, indent=2))

    images_by_site: dict[str, list[dict]] = defaultdict(list)
    for img in images:
        images_by_site[img["site_id"]].append(img)

    articles_by_site: dict[str, list[dict]] = defaultdict(list)
    for a in articles:
        articles_by_site[a["site_id"]].append(a)

    def _block_info(s: dict) -> tuple[str, str | None, str | None]:
        return block_info(s, articles_by_site)

    publisher_urls = load_publisher_urls()
    sites_out = []
    for s in sites:
        site_imgs = images_by_site.get(s["site_id"], [])
        ok_imgs = [i for i in site_imgs if i["http_status"] == 200]
        effective_status, block_phase, block_vendor = _block_info(s)
        # For blocked sites, compute the dominant article HTTP code (most frequent
        # non-200) as a lightweight diagnostic hint for the detail page.
        block_http_codes: dict[str, int] = {}
        if effective_status == "blocked" and block_phase == "article":
            for a in articles_by_site.get(s["site_id"], []):
                code = a.get("http_status") or 0
                if code != 200:
                    block_http_codes[str(code)] = block_http_codes.get(str(code), 0) + 1
        sites_out.append({
            "site_id": s["site_id"],
            "site_name": s["site_name"],
            "url": publisher_urls.get(s["site_id"], ""),
            "country": s["country"],
            "category": s["category"],
            "status": effective_status,
            "block_phase": block_phase,
            "block_vendor": block_vendor,
            "block_http_codes": block_http_codes,
            "discovery_strategy": s["discovery_strategy"],
            "sitemap_url_used": s["sitemap_url_used"],
            "articles_sampled": s["articles_sampled"],
            "images_analysed": s["images_analysed"],
            "mean_iptc_score": s["mean_iptc_score"],
            "pct_with_iptc": (
                round(100.0 * sum(1 for i in ok_imgs if i["image_url_hash"] in field_hashes) / len(ok_imgs), 1)
                if ok_imgs else 0.0
            ),
            "cdn_breakdown": _counter([i["cdn_provider"] for i in ok_imgs]),
        })
    sites_out.sort(key=lambda x: x["mean_iptc_score"], reverse=True)
    (out_dir / "sites.json").write_text(json.dumps(sites_out, indent=2))

    weight_for = {label: weight for label, _aliases, weight in ALL_FIELDS}
    scored_labels = {label for label, _aliases, _w in SCORED_FIELDS}

    # Per-site field aggregates for the publisher detail pages. Join
    # metadata_fields → images on image_url_hash to get site_id.
    site_by_hash = {i["image_url_hash"]: i["site_id"] for i in images}
    site_field_agg: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"present": 0, "total": 0})
    )
    for f in fields:
        sid = site_by_hash.get(f["image_url_hash"])
        if sid is None:
            continue
        bucket = site_field_agg[sid][f["field_name"]]
        bucket["total"] += 1
        if f["has_value"]:
            bucket["present"] += 1
    fields_by_site_out: dict[str, list[dict]] = {}
    for sid, per_field in site_field_agg.items():
        rows = []
        for name, data in per_field.items():
            total = data["total"]
            rows.append({
                "field": name,
                "present": data["present"],
                "total": total,
                "pct": round(100.0 * data["present"] / total, 1) if total else 0.0,
                "scored": name in scored_labels,
            })
        # Stable order: scored fields first (in their canonical order from
        # SCORED_FIELDS), then tracked fields alphabetically.
        scored_order = {label: i for i, (label, _, _) in enumerate(SCORED_FIELDS)}
        rows.sort(key=lambda r: (0 if r["scored"] else 1,
                                 scored_order.get(r["field"], 99),
                                 r["field"]))
        fields_by_site_out[sid] = rows
    (out_dir / "fields_by_site.json").write_text(
        json.dumps(fields_by_site_out, indent=2)
    )

    field_agg: dict[str, dict] = defaultdict(lambda: {"present": 0, "total": 0})
    for f in fields:
        field_agg[f["field_name"]]["total"] += 1
        if f["has_value"]:
            field_agg[f["field_name"]]["present"] += 1
    fields_out = [
        {
            "field": name,
            "present": data["present"],
            "total": data["total"],
            "pct": round(100.0 * data["present"] / data["total"], 1) if data["total"] else 0.0,
            "weight": weight_for.get(name, 0),
            "weight_pct": (
                round(100.0 * weight_for.get(name, 0) / TOTAL_WEIGHT, 1)
                if TOTAL_WEIGHT else 0.0
            ),
            "scored": name in scored_labels,
        }
        for name, data in sorted(field_agg.items(), key=lambda x: -x[1]["present"])
    ]
    (out_dir / "fields.json").write_text(json.dumps(fields_out, indent=2))

    cdn_breakdown = _counter([i["cdn_provider"] for i in images if i["http_status"] == 200])
    optimizer_cross: dict[str, dict] = defaultdict(lambda: {"images": 0, "stripped": 0})
    for img in images:
        if img["http_status"] != 200:
            continue
        cdn = img["cdn_provider"]
        optimizer_cross[cdn]["images"] += 1
        if img["image_url_hash"] not in field_hashes:
            optimizer_cross[cdn]["stripped"] += 1
    cdn_out = {
        "providers": cdn_breakdown,
        "by_provider": [
            {
                "provider": cdn,
                "images": data["images"],
                "stripped": data["stripped"],
                "pct_stripped": round(100.0 * data["stripped"] / data["images"], 1) if data["images"] else 0.0,
            }
            for cdn, data in sorted(optimizer_cross.items(), key=lambda x: -x[1]["images"])
        ],
    }
    (out_dir / "cdn.json").write_text(json.dumps(cdn_out, indent=2))

    # C2PA-specific aggregations for the /c2pa/ page.
    site_name_by_id = {s["site_id"]: s["site_name"] for s in sites}
    site_country_by_id = {s["site_id"]: s["country"] for s in sites}
    c2pa_images = [img for img in images if img.get("has_c2pa")]
    signer_counts: dict[str, int] = defaultdict(int)
    state_counts: dict[str, int] = defaultdict(int)
    bucket_counts: dict[str, int] = defaultdict(int)
    sites_with_c2pa: dict[str, int] = defaultdict(int)
    for img in c2pa_images:
        signer = img.get("c2pa_manifest_signer") or "(unknown signer)"
        state = img.get("c2pa_validation_status") or "(unknown state)"
        failure_codes = list(img.get("c2pa_failure_codes") or [])
        signer_counts[signer] += 1
        state_counts[state] += 1
        bucket_counts[_classify_c2pa(state, failure_codes)] += 1
        sites_with_c2pa[img["site_id"]] += 1
    c2pa_out = {
        "image_count_total": len(images),
        "image_count_with_c2pa": len(c2pa_images),
        "pct_with_c2pa": summary["pct_with_c2pa"],
        "by_outcome": [
            {"outcome": b, "images": bucket_counts.get(b, 0)}
            for b in ("valid", "modified", "expired", "untrusted_issuer", "other_invalid")
        ],
        "by_signer": [
            {"signer": k, "images": v}
            for k, v in sorted(signer_counts.items(), key=lambda x: -x[1])
        ],
        "by_validation_state": [
            {"state": k, "images": v}
            for k, v in sorted(state_counts.items(), key=lambda x: -x[1])
        ],
        "top_sites": [
            {
                "site_id": sid,
                "site_name": site_name_by_id.get(sid, sid),
                "country": site_country_by_id.get(sid, ""),
                "images_with_c2pa": n,
            }
            for sid, n in sorted(sites_with_c2pa.items(), key=lambda x: -x[1])
        ],
    }
    (out_dir / "c2pa.json").write_text(json.dumps(c2pa_out, indent=2))

    # Per-site C2PA breakdown for the publisher detail pages. Only sites
    # with at least one C2PA-bearing image get an entry.
    per_site_c2pa: dict[str, dict] = {}
    for sid in sites_with_c2pa:
        site_imgs = [img for img in c2pa_images if img["site_id"] == sid]
        s_signer: dict[str, int] = defaultdict(int)
        s_state: dict[str, int] = defaultdict(int)
        s_bucket: dict[str, int] = defaultdict(int)
        for img in site_imgs:
            s_signer[img.get("c2pa_manifest_signer") or "(unknown signer)"] += 1
            s_state[img.get("c2pa_validation_status") or "(unknown state)"] += 1
            s_bucket[_classify_c2pa(
                img.get("c2pa_validation_status"),
                list(img.get("c2pa_failure_codes") or []),
            )] += 1
        per_site_c2pa[sid] = {
            "image_count_with_c2pa": len(site_imgs),
            "by_outcome": [
                {"outcome": b, "images": s_bucket.get(b, 0)}
                for b in ("valid", "modified", "expired", "untrusted_issuer", "other_invalid")
                if s_bucket.get(b, 0) > 0
            ],
            "by_signer": [
                {"signer": k, "images": v}
                for k, v in sorted(s_signer.items(), key=lambda x: -x[1])
            ],
            "by_validation_state": [
                {"state": k, "images": v}
                for k, v in sorted(s_state.items(), key=lambda x: -x[1])
            ],
        }
    (out_dir / "c2pa_by_site.json").write_text(json.dumps(per_site_c2pa, indent=2))

    # DigitalSourceType — per-bucket counts from both XMP and C2PA, plus a
    # raw URI breakdown. Buckets come from config/dst_vocab.yaml (editable,
    # so re-bucketing doesn't require a re-crawl).
    dst_vocab = load_dst_buckets()
    by_bucket_iptc: dict[str, int] = defaultdict(int)
    by_bucket_c2pa: dict[str, int] = defaultdict(int)
    by_uri_iptc: dict[str, int] = defaultdict(int)
    by_uri_c2pa: dict[str, int] = defaultdict(int)
    images_with_dst_iptc = 0
    images_with_dst_c2pa = 0
    # Per-site tallies, populated alongside the global ones.
    site_dst: dict[str, dict] = defaultdict(lambda: {
        "images_with_dst_iptc": 0,
        "images_with_dst_c2pa": 0,
        "by_bucket_iptc": defaultdict(int),
        "by_bucket_c2pa": defaultdict(int),
        "by_uri_iptc": defaultdict(int),
        "by_uri_c2pa": defaultdict(int),
    })
    for img in images:
        if img.get("http_status") != 200:
            continue
        sid = img["site_id"]
        dst_iptc = img.get("dst_iptc")
        if dst_iptc:
            images_with_dst_iptc += 1
            by_uri_iptc[dst_iptc] += 1
            by_bucket_iptc[bucket_for_dst(dst_iptc, dst_vocab)] += 1
            site_dst[sid]["images_with_dst_iptc"] += 1
            site_dst[sid]["by_uri_iptc"][dst_iptc] += 1
            site_dst[sid]["by_bucket_iptc"][bucket_for_dst(dst_iptc, dst_vocab)] += 1
        for uri in (img.get("dst_c2pa") or []):
            images_with_dst_c2pa += 1  # counts every occurrence; an image with N C2PA DSTs contributes N
            by_uri_c2pa[uri] += 1
            by_bucket_c2pa[bucket_for_dst(uri, dst_vocab)] += 1
            site_dst[sid]["images_with_dst_c2pa"] += 1
            site_dst[sid]["by_uri_c2pa"][uri] += 1
            site_dst[sid]["by_bucket_c2pa"][bucket_for_dst(uri, dst_vocab)] += 1

    def _ordered_buckets(d: dict[str, int]) -> list[dict]:
        return [
            {"bucket": k, "images": v}
            for k, v in sorted(d.items(), key=lambda x: -x[1])
        ]

    def _ordered_uris(d: dict[str, int]) -> list[dict]:
        return [
            {"uri": k, "term": _dst_short_term(k), "bucket": bucket_for_dst(k, dst_vocab), "images": v}
            for k, v in sorted(d.items(), key=lambda x: -x[1])
        ]

    valid_images = sum(1 for i in images if i.get("http_status") == 200)
    dst_out = {
        "image_count_total": len(images),
        "image_count_valid": valid_images,
        "images_with_dst_iptc": images_with_dst_iptc,
        "images_with_dst_c2pa": images_with_dst_c2pa,
        "pct_with_dst_iptc": (
            round(100.0 * images_with_dst_iptc / valid_images, 2) if valid_images else 0.0
        ),
        "by_bucket_iptc": _ordered_buckets(by_bucket_iptc),
        "by_bucket_c2pa": _ordered_buckets(by_bucket_c2pa),
        "by_uri_iptc": _ordered_uris(by_uri_iptc),
        "by_uri_c2pa": _ordered_uris(by_uri_c2pa),
    }
    (out_dir / "dst.json").write_text(json.dumps(dst_out, indent=2))

    # Per-site DST breakdown for publisher detail pages.
    dst_by_site_out: dict[str, dict] = {}
    for sid, agg in site_dst.items():
        if not (agg["images_with_dst_iptc"] or agg["images_with_dst_c2pa"]):
            continue
        dst_by_site_out[sid] = {
            "images_with_dst_iptc": agg["images_with_dst_iptc"],
            "images_with_dst_c2pa": agg["images_with_dst_c2pa"],
            "by_bucket_iptc": _ordered_buckets(agg["by_bucket_iptc"]),
            "by_bucket_c2pa": _ordered_buckets(agg["by_bucket_c2pa"]),
            "by_uri_iptc": _ordered_uris(agg["by_uri_iptc"]),
            "by_uri_c2pa": _ordered_uris(agg["by_uri_c2pa"]),
        }
    (out_dir / "dst_by_site.json").write_text(json.dumps(dst_by_site_out, indent=2))

    # ─── AI-policy / opt-out aggregations ────────────────────────────────────
    # Inputs: SiteRow optout fields + robots_analysis.parquet (per-(site,UA))
    # + ImageRow noai_tokens / cawg_training_mining_json.
    #
    # Denominator rules:
    #  * Site-wide signals (tdmrep, ai.txt, RSL, trust.txt, robots-AI matrix):
    #    only sites where the optout probes actually ran. That's every site
    #    EXCEPT statuses timeout/error/unreachable — those bail before the
    #    probe step and would otherwise inflate "no" counts.
    #  * Image-level signals (noai, CAWG, IPTC PLUS:DataMining): only images
    #    we successfully fetched (http_status == 200).
    robots_rows = read(run_dir / "robots_analysis.parquet")
    probed_sites = [s for s in sites if s["status"] not in ("timeout", "error", "unreachable")]
    n_probed = len(probed_sites)
    ok_imgs = [i for i in images if i["http_status"] == 200]
    n_imgs = len(ok_imgs)

    def _pct(num: int, den: int) -> float:
        return round(100.0 * num / den, 1) if den else 0.0

    sites_with_tdmrep = sum(1 for s in probed_sites if s["has_tdmrep"])
    sites_with_ai_txt = sum(1 for s in probed_sites if s["has_ai_txt"])
    sites_with_rsl = sum(1 for s in probed_sites if s["has_rsl"])
    sites_with_trust_dta_no = sum(
        1 for s in probed_sites
        if s["has_trust_txt"] and (s["trust_txt_datatraining"] or "").lower() == "no"
    )
    sites_blocking_any_ai = sum(1 for s in probed_sites if s["ai_bots_blocked_count"] > 0)
    # Content Signals headline metric: sites that emit any Content-Signal:
    # directive at all. (The per-signal values — ai-train=no etc. — show up
    # on the drill-down page.) The probe records the presence flag even when
    # only `search=yes` is asserted, because the publisher has nonetheless
    # adopted the mechanism.
    sites_with_content_signals = sum(1 for s in probed_sites if s.get("has_content_signals"))

    # Per-page TDMRep meta tag: aggregate from the articles table up to site
    # level. A site is flagged if any sampled article carried
    # <meta name="tdm-reservation" content="1">. The per-article count is
    # carried into the drill-down list so publishers can see how consistently
    # the tag appears across their sampled articles.
    probed_site_ids = {s["site_id"] for s in probed_sites}
    articles_reserved_by_site: dict[str, int] = {}
    articles_total_by_site: dict[str, int] = {}
    for a in articles:
        sid = a["site_id"]
        if sid not in probed_site_ids:
            continue
        articles_total_by_site[sid] = articles_total_by_site.get(sid, 0) + 1
        if a.get("tdm_reservation") == 1:
            articles_reserved_by_site[sid] = articles_reserved_by_site.get(sid, 0) + 1
    sites_with_tdmrep_meta = len(articles_reserved_by_site)

    # Image-level signals are reported at site granularity ("% of probed sites
    # where ≥1 sampled image carried the signal") rather than image granularity
    # ("% of all sampled images"). Rationale: a single image with the field
    # demonstrates that the publisher *can* express the signal at all, which is
    # what the dashboard is measuring. Per-image percentages are dominated by
    # how many images each site happens to publish and dilute the signal.
    site_by_image_hash: dict[str, str] = {i["image_url_hash"]: i["site_id"] for i in ok_imgs}

    # noai/noimageai/noml are AI-specific tokens — coined by DeviantArt in 2022
    # specifically as AI-opt-out directives. noarchive/nosnippet are classic
    # search-snippet directives that single vendors (Bing/Copilot, Google
    # respectively) have post-hoc reinterpreted to also block AI use. Mixing
    # the two in a single headline dilutes the signal: a site setting
    # noarchive for cache-control reasons looks identical to one expressing
    # AI intent. Report them as two independent signals.
    _AI_SPECIFIC = {"noai", "noimageai", "noml"}
    _AI_IMPLICATED = {"noarchive", "nosnippet"}
    sites_with_noai = {
        i["site_id"] for i in ok_imgs
        if set(i.get("noai_tokens") or ()) & _AI_SPECIFIC
    }
    sites_with_noarchive = {
        i["site_id"] for i in ok_imgs
        if set(i.get("noai_tokens") or ()) & _AI_IMPLICATED
    }
    sites_with_cawg = {i["site_id"] for i in ok_imgs if i.get("cawg_training_mining_json")}
    # IPTC PLUS:DataMining presence comes from metadata_fields.parquet (one row
    # per (image, field, has_value)). Project image_url_hash → site_id via the
    # ok_imgs map; some metadata rows belong to non-200 image fetches and must
    # be dropped to keep the denominator honest.
    sites_with_iptc_datamining = {
        site_by_image_hash[f["image_url_hash"]]
        for f in fields
        if f["field_name"] == "DataMining"
        and f["has_value"]
        and f["image_url_hash"] in site_by_image_hash
    }

    signals_out = [
        {
            "key": "robots-ai", "scope": "site",
            "label": "robots.txt — any AI bot blocked",
            "note": "Blocks at least one known AI/scraper UA",
            "num": sites_blocking_any_ai, "denom": n_probed,
            "pct": _pct(sites_blocking_any_ai, n_probed),
        },
        {
            "key": "tdmrep", "scope": "site",
            "label": "/.well-known/tdmrep.json",
            "note": "TDM Reservation Protocol (EU DSM Art. 4)",
            "num": sites_with_tdmrep, "denom": n_probed,
            "pct": _pct(sites_with_tdmrep, n_probed),
        },
        {
            "key": "tdmrep-meta", "scope": "site",
            "label": "TDMRep — per-page <meta>",
            "note": "<meta name=\"tdm-reservation\" content=\"1\"> on ≥1 article",
            "num": sites_with_tdmrep_meta, "denom": n_probed,
            "pct": _pct(sites_with_tdmrep_meta, n_probed),
        },
        {
            "key": "rsl", "scope": "site",
            "label": "RSL — License: in robots.txt",
            "note": "Really Simple Licensing (rslstandard.org)",
            "num": sites_with_rsl, "denom": n_probed,
            "pct": _pct(sites_with_rsl, n_probed),
        },
        {
            "key": "ai-txt", "scope": "site",
            "label": "/ai.txt",
            "note": "Spawning AI consent proposal",
            "num": sites_with_ai_txt, "denom": n_probed,
            "pct": _pct(sites_with_ai_txt, n_probed),
        },
        {
            "key": "content-signals", "scope": "site",
            "label": "robots.txt — Content-Signal",
            "note": "Cloudflare Content Signals (contentsignals.org)",
            "num": sites_with_content_signals, "denom": n_probed,
            "pct": _pct(sites_with_content_signals, n_probed),
        },
        {
            "key": "trust-txt-dta", "scope": "site",
            "label": "trust.txt — datatrainingallowed=no",
            "note": "JournalList trust.txt opt-out directive",
            "num": sites_with_trust_dta_no, "denom": n_probed,
            "pct": _pct(sites_with_trust_dta_no, n_probed),
        },
        {
            "key": "noai-meta", "scope": "site",
            "label": "noai / noimageai meta robots",
            "note": "AI-specific tokens on X-Robots-Tag or <meta name=\"robots\">",
            "num": len(sites_with_noai), "denom": n_probed,
            "pct": _pct(len(sites_with_noai), n_probed),
        },
        {
            "key": "noarchive-meta", "scope": "site",
            "label": "noarchive / nosnippet meta robots",
            "note": "Honoured as AI-opt-out by Bing/Copilot (noarchive) and Google (nosnippet)",
            "num": len(sites_with_noarchive), "denom": n_probed,
            "pct": _pct(len(sites_with_noarchive), n_probed),
        },
        {
            "key": "iptc-datamining", "scope": "site",
            "label": "IPTC PLUS:DataMining",
            "note": "XMP-plus:DataMining on ≥1 sampled image",
            "num": len(sites_with_iptc_datamining), "denom": n_probed,
            "pct": _pct(len(sites_with_iptc_datamining), n_probed),
        },
        {
            "key": "cawg-training-mining", "scope": "site",
            "label": "CAWG Training and Data Mining Assertion",
            "note": "cawg.training-mining assertion in ≥1 sampled image's C2PA manifest",
            "num": len(sites_with_cawg), "denom": n_probed,
            "pct": _pct(len(sites_with_cawg), n_probed),
        },
    ]

    # Per-UA block-rate matrix. Group rows by user_agent; count "disallowed".
    ua_groups: dict[str, dict] = {}
    sites_in_matrix: set[str] = set()
    for r in robots_rows:
        sites_in_matrix.add(r["site_id"])
        slot = ua_groups.setdefault(r["user_agent"], {
            "ua": r["user_agent"], "operator": r["operator"],
            "blocked": 0, "total": 0,
        })
        slot["total"] += 1
        if r["status"] == "disallowed":
            slot["blocked"] += 1
    bots_out = [
        {
            "ua": g["ua"], "operator": g["operator"],
            "blocked": g["blocked"], "total": g["total"],
            "pct_blocked": _pct(g["blocked"], g["total"]),
        }
        for g in ua_groups.values()
    ]
    bots_out.sort(key=lambda x: (-x["pct_blocked"], x["ua"].lower()))

    # Distribution of "how many UAs each site blocks".
    blocked_by_site: dict[str, int] = {sid: 0 for sid in sites_in_matrix}
    for r in robots_rows:
        if r["status"] == "disallowed":
            blocked_by_site[r["site_id"]] += 1
    # Bucket boundaries chosen to give a readable five-bar histogram.
    n_uas = max((g["total"] for g in ua_groups.values()), default=0)
    buckets = [(0, 0), (1, 5), (6, 10), (11, 20), (21, n_uas)]
    bucket_out = []
    for lo, hi in buckets:
        if hi < lo:
            continue
        count = sum(1 for n in blocked_by_site.values() if lo <= n <= hi)
        label = "0 (no AI blocks)" if lo == 0 and hi == 0 else (
            f"{lo}" if lo == hi else f"{lo}–{hi}"
        )
        bucket_out.append({
            "range": label, "lo": lo, "hi": hi,
            "count": count, "pct": _pct(count, len(sites_in_matrix)),
        })

    # Signal convergence — when a site uses signal A, do they also use B?
    by_site = {s["site_id"]: s for s in probed_sites}
    def _frac(predicate_a, predicate_b) -> tuple[int, int, float]:
        a_sites = [sid for sid, s in by_site.items() if predicate_a(s)]
        if not a_sites:
            return 0, 0, 0.0
        both = sum(1 for sid in a_sites if predicate_b(by_site[sid]))
        return both, len(a_sites), _pct(both, len(a_sites))

    # Look up which sites block specific UAs.
    site_blocks: dict[str, set[str]] = {}
    for r in robots_rows:
        if r["status"] == "disallowed":
            site_blocks.setdefault(r["site_id"], set()).add(r["user_agent"])

    def _blocks(ua: str):
        return lambda s, ua=ua: ua in site_blocks.get(s["site_id"], set())

    convergence_out = []
    for a_label, a_pred, b_label, b_pred in [
        ("blocks GPTBot",   _blocks("GPTBot"),
         "blocks ClaudeBot", _blocks("ClaudeBot")),
        ("blocks GPTBot",   _blocks("GPTBot"),
         "has tdmrep.json",  lambda s: s["has_tdmrep"]),
        ("has tdmrep.json", lambda s: s["has_tdmrep"],
         "blocks ≥1 AI UA", lambda s: s["ai_bots_blocked_count"] > 0),
        ("has ai.txt",      lambda s: s["has_ai_txt"],
         "has tdmrep.json",  lambda s: s["has_tdmrep"]),
        ("has RSL License", lambda s: s["has_rsl"],
         "blocks ≥1 AI UA", lambda s: s["ai_bots_blocked_count"] > 0),
    ]:
        both, denom, pct = _frac(a_pred, b_pred)
        convergence_out.append({
            "a": a_label, "b": b_label,
            "a_count": denom, "both": both, "pct": pct,
        })

    # Per-signal site/image lists for the drill-down pages
    # (/ai-policy/<signal>/). Each entry is the minimum the page needs to
    # render a row: site identity + a signal-specific `extra` payload (URL,
    # value, count, etc.).
    site_meta = {s["site_id"]: s for s in probed_sites}

    def _site_basic(sid: str) -> dict:
        s = site_meta.get(sid, {})
        return {
            "site_id": sid,
            "site_name": s.get("site_name", sid),
            "country": s.get("country", ""),
        }

    # Site-level signals: read straight off SiteRow.
    tdmrep_sites = [
        {**_site_basic(s["site_id"]),
         "url": s["site_name"] and f"{s['robots_url'].rsplit('/', 1)[0]}/.well-known/tdmrep.json"}
        for s in probed_sites if s["has_tdmrep"]
    ]
    ai_txt_sites = [
        {**_site_basic(s["site_id"]),
         "url": f"{s['robots_url'].rsplit('/', 1)[0]}/ai.txt"}
        for s in probed_sites if s["has_ai_txt"]
    ]
    rsl_sites = [
        {**_site_basic(s["site_id"]),
         "license_urls": list(s["rsl_license_urls"] or [])}
        for s in probed_sites if s["has_rsl"]
    ]
    trust_dta_sites = [
        {**_site_basic(s["site_id"]),
         "value": s["trust_txt_datatraining"]}
        for s in probed_sites
        if s["has_trust_txt"] and (s["trust_txt_datatraining"] or "").lower() == "no"
    ]
    # TDMRep per-page meta drill-down: site + how many of its sampled
    # articles carry tdm-reservation=1, sorted by that count descending.
    tdmrep_meta_sites = sorted(
        [
            {**_site_basic(sid),
             "articles": n,
             "articles_total": articles_total_by_site.get(sid, 0)}
            for sid, n in articles_reserved_by_site.items()
        ],
        key=lambda x: (-x["articles"], x["site_name"].lower()),
    )

    # Content-Signals drill-down: one row per site with its three known
    # signal values. Sort so publishers opting out of AI-training appear first.
    def _cs_sort_key(s: dict) -> tuple:
        ai_train = (s.get("content_signal_ai_train") or "").lower()
        return (0 if ai_train == "no" else 1, s.get("site_name", "").lower())
    content_signals_sites = sorted(
        [
            {**_site_basic(s["site_id"]),
             "ai_train": s.get("content_signal_ai_train"),
             "ai_input": s.get("content_signal_ai_input"),
             "search": s.get("content_signal_search")}
            for s in probed_sites if s.get("has_content_signals")
        ],
        key=_cs_sort_key,
    )
    # robots-ai: sites that block ≥1 UA, sorted by block count desc.
    robots_ai_sites = sorted(
        [
            {**_site_basic(s["site_id"]),
             "blocked_count": s["ai_bots_blocked_count"],
             "blocked_uas": sorted(site_blocks.get(s["site_id"], set()))}
            for s in probed_sites if s["ai_bots_blocked_count"] > 0
        ],
        key=lambda x: (-x["blocked_count"], x["site_name"].lower()),
    )

    # Image-level signals: aggregate to per-site counts (how many sampled
    # images on each site carried the signal). site_by_image_hash is built
    # earlier (around the signal aggregation block) — reused here for the
    # IPTC PLUS:DataMining lookup further down.

    def _by_site_image_signal(pred) -> list[dict]:
        per_site: dict[str, int] = {}
        for i in ok_imgs:
            if pred(i):
                per_site[i["site_id"]] = per_site.get(i["site_id"], 0) + 1
        return sorted(
            [{**_site_basic(sid), "images": n} for sid, n in per_site.items()],
            key=lambda x: (-x["images"], x["site_name"].lower()),
        )

    # IPTC PLUS:DataMining is per-image-field, so we project the metadata_fields
    # rows down to site via the image hash → site_id map.
    dm_per_site: dict[str, int] = {}
    for f in fields:
        if f["field_name"] == "DataMining" and f["has_value"]:
            sid = site_by_image_hash.get(f["image_url_hash"])
            if sid:
                dm_per_site[sid] = dm_per_site.get(sid, 0) + 1
    datamining_sites = sorted(
        [{**_site_basic(sid), "images": n} for sid, n in dm_per_site.items()],
        key=lambda x: (-x["images"], x["site_name"].lower()),
    )

    noai_sites = _by_site_image_signal(
        lambda i: bool(set(i.get("noai_tokens") or ()) & _AI_SPECIFIC)
    )
    noarchive_sites = _by_site_image_signal(
        lambda i: bool(set(i.get("noai_tokens") or ()) & _AI_IMPLICATED)
    )
    cawg_sites = _by_site_image_signal(lambda i: bool(i.get("cawg_training_mining_json")))

    by_signal = {
        "robots-ai": robots_ai_sites,
        "tdmrep": tdmrep_sites,
        "tdmrep-meta": tdmrep_meta_sites,
        "rsl": rsl_sites,
        "ai-txt": ai_txt_sites,
        "trust-txt-dta": trust_dta_sites,
        "content-signals": content_signals_sites,
        "noai-meta": noai_sites,
        "noarchive-meta": noarchive_sites,
        "iptc-datamining": datamining_sites,
        "cawg-training-mining": cawg_sites,
    }

    ai_policy_out = {
        "n_sites": n_probed,
        "n_images": n_imgs,
        "signals": signals_out,
        "ai_bots": bots_out,
        "block_buckets": bucket_out,
        "convergence": convergence_out,
        "by_signal": by_signal,
    }
    (out_dir / "ai_policy.json").write_text(json.dumps(ai_policy_out, indent=2))

    # Per-site sample articles + their lead images, for the publisher pages.
    # One row per article, up to 20, newest-first. Joins articles ↔ images
    # via sha1(article_url) = article_url_hash, and articles ↔ metadata_fields
    # via that-same image's image_url_hash to assemble the Four-Cs presence
    # checks for the row.
    images_by_article: dict[str, dict] = {}
    for img in images:
        # Each article was matched 1:1 to a lead image at crawl time, so we
        # only need the first one we see per article_url_hash.
        images_by_article.setdefault(img["article_url_hash"], img)

    # Per-image field-presence map. Stored as a set of "label" → True so the
    # exported list is compact (only fields that ARE present are listed).
    presence_by_image: dict[str, set[str]] = defaultdict(set)
    for f in fields:
        if f["has_value"]:
            presence_by_image[f["image_url_hash"]].add(f["field_name"])

    samples_by_site: dict[str, list[dict]] = defaultdict(list)
    for art in articles:
        art_hash = hashlib.sha1(art["article_url"].encode()).hexdigest()
        img = images_by_article.get(art_hash)
        row = {
            "article_url": art["article_url"],
            "title": _clean_title(art.get("title")),
            "publication_date": fmt_dt(art.get("publication_date")),
            "article_http_status": art.get("http_status"),
        }
        if img is not None:
            present_fields = sorted(presence_by_image.get(img["image_url_hash"], set()))
            tags_json = img.get("iptc_xmp_tags_json")
            try:
                metadata = json.loads(tags_json) if tags_json else {}
            except (TypeError, ValueError):
                metadata = {}
            row.update({
                "image_url": img["image_url"],
                "image_http_status": img.get("http_status"),
                "iptc_score": img.get("iptc_score") or 0.0,
                "has_exif": bool(img.get("has_exif")),
                "has_iptc_iim": bool(img.get("has_iptc_iim")),
                "has_iptc_xmp": bool(img.get("has_iptc_xmp")),
                "has_c2pa": bool(img.get("has_c2pa")),
                "c2pa_signer": img.get("c2pa_manifest_signer"),
                "c2pa_validation_status": img.get("c2pa_validation_status"),
                "cdn_provider": img.get("cdn_provider") or "unknown",
                "cdn_optimizer_active": img.get("cdn_optimizer_active"),
                "mime_type": img.get("mime_type"),
                "width": img.get("width"),
                "height": img.get("height"),
                "file_size_bytes": img.get("file_size_bytes"),
                "dst_iptc": img.get("dst_iptc"),
                "dst_c2pa": list(img.get("dst_c2pa") or []),
                # Compact: only fields that ARE present are listed. The full
                # set (scored + tracked) comes from scoring.yaml; anything
                # absent from this list is missing for this image.
                "present_fields": present_fields,
                # Raw exiftool key/value pairs we kept for this image: all
                # IPTC + XMP content plus the EXIF/TIFF tags (IFD0:*)
                # that earned score. Empty {} when nothing was carried (or for
                # runs crawled before EXIF evidence was stored). The panel
                # partitions these by group prefix for display.
                "metadata": metadata,
            })
        samples_by_site[art["site_id"]].append(row)

    # Sort by publication_date desc (newest first), cap at 20 per site.
    for sid, rows in samples_by_site.items():
        rows.sort(key=lambda r: r.get("publication_date") or "", reverse=True)
        samples_by_site[sid] = rows[:20]
    # Written compactly: this file is large (sample URLs add up) and is
    # machine-consumed by the Astro build. The pretty pyarrow data is still
    # available under data/runs/<date>/ for anyone reading it by hand.
    (out_dir / "samples_by_site.json").write_text(
        json.dumps(dict(samples_by_site), separators=(",", ":"))
    )

    history = []
    history_by_site: dict[str, list[dict]] = defaultdict(list)
    history_by_country: dict[str, list[dict]] = defaultdict(list)
    # Per-site per-scored-field time series. Restricted to the Four Cs to keep
    # the JSON small enough to ship inline with the Astro build.
    history_fields_by_site: dict[str, dict[str, list[dict]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for d in sorted(all_runs):
        rs = read(d / "runs.parquet")
        if not rs:
            continue
        run = rs[0]
        run_started = fmt_dt(run["started_at"])

        # Per-run aggregates from per-image rows
        scores = []
        c2pa_count = 0
        iptc_count = 0
        valid_image_count = 0
        c2pa_outcome_counts: dict[str, int] = defaultdict(int)
        imgs_path = d / "images.parquet"
        run_field_hashes = images_with_any_field(read(d / "metadata_fields.parquet"))
        run_images: list[dict] = []
        if imgs_path.exists():
            run_images = read(imgs_path)
            for r in run_images:
                if r["http_status"] != 200:
                    continue
                valid_image_count += 1
                scores.append(r["iptc_score"])
                if r["image_url_hash"] in run_field_hashes:
                    iptc_count += 1
                if r.get("has_c2pa"):
                    c2pa_count += 1
                    bucket = _classify_c2pa(
                        r.get("c2pa_validation_status"),
                        list(r.get("c2pa_failure_codes") or []),
                    )
                    c2pa_outcome_counts[bucket] += 1
        run_counts = derived_run_counts(
            read(d / "sites.parquet"), read(d / "articles.parquet"), run_images,
        )
        history.append({
            "run_id": run["run_id"],
            "started_at": run_started,
            "site_count": run_counts["site_count_succeeded"],
            "image_count": run_counts["image_count"],
            "mean_score": round(mean(scores), 2) if scores else 0.0,
            "images_with_iptc": iptc_count,
            "pct_with_iptc": (
                round(100.0 * iptc_count / valid_image_count, 1)
                if valid_image_count else 0.0
            ),
            "images_with_c2pa": c2pa_count,
            "pct_with_c2pa": (
                round(100.0 * c2pa_count / valid_image_count, 3)
                if valid_image_count else 0.0
            ),
            "c2pa_outcomes": dict(c2pa_outcome_counts),
        })

        # Per-site and per-country series from this run's sites.parquet
        run_sites = read(d / "sites.parquet")
        country_buckets: dict[str, list[float]] = defaultdict(list)
        for s in run_sites:
            if s["status"] != "ok" or s["images_analysed"] == 0:
                continue
            history_by_site[s["site_id"]].append({
                "x": run_started, "y": s["mean_iptc_score"],
            })
            country_buckets[s["country"]].append(s["mean_iptc_score"])
        for cc, vals in country_buckets.items():
            history_by_country[cc].append({
                "x": run_started, "y": round(mean(vals), 2),
            })

        # Per-site per-Four-C series. Join this run's metadata_fields with
        # images on image_url_hash to get site_id, then aggregate per
        # (site_id, field_name) for scored fields only.
        fields_path = d / "metadata_fields.parquet"
        if fields_path.exists() and run_images:
            run_site_by_hash = {i["image_url_hash"]: i["site_id"] for i in run_images}
            agg: dict[tuple[str, str], dict[str, int]] = defaultdict(
                lambda: {"present": 0, "total": 0}
            )
            for f in read(fields_path):
                if f["field_name"] not in scored_labels:
                    continue
                sid = run_site_by_hash.get(f["image_url_hash"])
                if sid is None:
                    continue
                cell = agg[(sid, f["field_name"])]
                cell["total"] += 1
                if f["has_value"]:
                    cell["present"] += 1
            for (sid, field_name), cell in agg.items():
                if cell["total"] == 0:
                    continue
                history_fields_by_site[sid][field_name].append({
                    "x": run_started,
                    "y": round(100.0 * cell["present"] / cell["total"], 1),
                })

    (out_dir / "history.json").write_text(json.dumps(history, indent=2))
    (out_dir / "history_by_site.json").write_text(
        json.dumps(dict(history_by_site), indent=2)
    )
    (out_dir / "history_by_country.json").write_text(
        json.dumps(dict(history_by_country), indent=2)
    )
    (out_dir / "history_fields_by_site.json").write_text(
        json.dumps({sid: dict(per_field) for sid, per_field in history_fields_by_site.items()}, indent=2)
    )

    # Per-run manifest for the public /dataset/ page.
    runs_index = []
    for run_path in all_runs:
        run_runs_pq = run_path / "runs.parquet"
        if not run_runs_pq.exists():
            continue
        rmeta = pq.read_table(run_runs_pq).to_pylist()[0]
        files = [
            {"name": f.name, "size_bytes": f.stat().st_size}
            for f in sorted(run_path.glob("*.parquet"))
        ]
        idx_counts = derived_run_counts(
            read(run_path / "sites.parquet"),
            read(run_path / "articles.parquet"),
            read(run_path / "images.parquet"),
        )
        runs_index.append({
            "run_id": rmeta["run_id"],
            "started_at": fmt_dt(rmeta["started_at"]),
            "ended_at": fmt_dt(rmeta["ended_at"]),
            "site_count_attempted": idx_counts["site_count_attempted"],
            "site_count_succeeded": idx_counts["site_count_succeeded"],
            "image_count": idx_counts["image_count"],
            "directory": run_path.name,
            "files": files,
        })
    runs_index.sort(key=lambda r: r["started_at"] or "", reverse=True)
    (out_dir / "runs_index.json").write_text(json.dumps(runs_index, indent=2))

    print(f"Exported run {summary['run_id']} → {out_dir}")
    print(f"  sites={len(sites_out)} images={len(images)} mean_score={summary['global_mean_score']}")


def _classify_c2pa(state: str | None, failure_codes: list[str]) -> str:
    """Bucket a C2PA validation result into a publishable outcome.

    - valid:            Reader returned state == "Valid"
    - modified:         data hash mismatch — bytes changed after signing
                        (the interesting "CDN stripped the manifest" case)
    - expired:          signing certificate expired (issuer-side problem)
    - untrusted_issuer: only failure is signingCredential.untrusted — purely
                        a trust-list configuration matter; signature itself fine
    - other_invalid:    anything else
    """
    if state == "Valid":
        return "valid"
    fail = set(failure_codes)
    if "assertion.dataHash.mismatch" in fail:
        return "modified"
    if "signingCredential.expired" in fail:
        return "expired"
    if fail == {"signingCredential.untrusted"}:
        return "untrusted_issuer"
    return "other_invalid"


def _counter(items: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in items:
        out[x] = out.get(x, 0) + 1
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-dir", type=Path, default=Path(__file__).resolve().parents[2] / "data" / "runs")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[2] / "site" / "src" / "data" / "latest")
    parser.add_argument("--run-id", default=None, help="Specific run dir name; defaults to most recent.")
    args = parser.parse_args()

    runs_dir: Path = args.runs_dir
    all_runs = sorted([p for p in runs_dir.iterdir() if p.is_dir()])
    if args.run_id:
        target = runs_dir / args.run_id
        if not target.exists():
            raise SystemExit(f"Run dir not found: {target}")
    else:
        target = latest_run_dir(runs_dir)

    export_run(target, args.out, all_runs)


if __name__ == "__main__":
    main()
