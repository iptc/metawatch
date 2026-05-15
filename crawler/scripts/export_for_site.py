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

Usage:
    python scripts/export_for_site.py [--runs-dir ../data/runs] [--out ../site/src/data]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean

import pyarrow.parquet as pq


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


def export_run(run_dir: Path, out_dir: Path, all_runs: list[Path]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    runs = read(run_dir / "runs.parquet")
    sites = read(run_dir / "sites.parquet")
    images = read(run_dir / "images.parquet")
    fields = read(run_dir / "metadata_fields.parquet")

    summary = {
        "run_id": runs[0]["run_id"] if runs else None,
        "started_at": fmt_dt(runs[0]["started_at"]) if runs else None,
        "ended_at": fmt_dt(runs[0]["ended_at"]) if runs else None,
        "site_count_attempted": runs[0]["site_count_attempted"] if runs else 0,
        "site_count_succeeded": runs[0]["site_count_succeeded"] if runs else 0,
        "site_count_robots_blocked": runs[0]["site_count_robots_blocked"] if runs else 0,
        "article_count": runs[0]["article_count"] if runs else 0,
        "image_count": runs[0]["image_count"] if runs else 0,
    }
    image_scores = [img["iptc_score"] for img in images if img["http_status"] == 200]
    summary["global_mean_score"] = round(mean(image_scores), 2) if image_scores else 0.0
    summary["images_with_iptc"] = sum(1 for img in images if img["has_iptc_iim"] or img["has_iptc_xmp"])
    summary["images_with_c2pa"] = sum(1 for img in images if img["has_c2pa"])
    summary["pct_with_iptc"] = (
        round(100.0 * summary["images_with_iptc"] / len(images), 1) if images else 0.0
    )
    summary["pct_with_c2pa"] = (
        round(100.0 * summary["images_with_c2pa"] / len(images), 2) if images else 0.0
    )

    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))

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

    sites_out = []
    for s in sites:
        site_imgs = images_by_site.get(s["site_id"], [])
        ok_imgs = [i for i in site_imgs if i["http_status"] == 200]
        sites_out.append({
            "site_id": s["site_id"],
            "site_name": s["site_name"],
            "country": s["country"],
            "category": s["category"],
            "status": s["status"],
            "discovery_strategy": s["discovery_strategy"],
            "sitemap_url_used": s["sitemap_url_used"],
            "articles_sampled": s["articles_sampled"],
            "images_analysed": s["images_analysed"],
            "mean_iptc_score": s["mean_iptc_score"],
            "pct_with_iptc": (
                round(100.0 * sum(1 for i in ok_imgs if i["has_iptc_iim"] or i["has_iptc_xmp"]) / len(ok_imgs), 1)
                if ok_imgs else 0.0
            ),
            "cdn_breakdown": _counter([i["cdn_provider"] for i in ok_imgs]),
        })
    sites_out.sort(key=lambda x: x["mean_iptc_score"], reverse=True)
    (out_dir / "sites.json").write_text(json.dumps(sites_out, indent=2))

    from pmd_crawler.scoring import FIELD_WEIGHTS, TOTAL_WEIGHT
    weight_for = {label: weight for label, _aliases, weight in FIELD_WEIGHTS}

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
            "weight_pct": round(100.0 * weight_for.get(name, 0) / TOTAL_WEIGHT, 1),
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
        if not (img["has_iptc_iim"] or img["has_iptc_xmp"]):
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

    history = []
    history_by_site: dict[str, list[dict]] = defaultdict(list)
    history_by_country: dict[str, list[dict]] = defaultdict(list)
    for d in sorted(all_runs):
        rs = read(d / "runs.parquet")
        if not rs:
            continue
        run = rs[0]
        run_started = fmt_dt(run["started_at"])

        # Per-run aggregates from per-image rows
        scores = []
        c2pa_count = 0
        valid_image_count = 0
        c2pa_outcome_counts: dict[str, int] = defaultdict(int)
        imgs_path = d / "images.parquet"
        if imgs_path.exists():
            for r in read(imgs_path):
                if r["http_status"] != 200:
                    continue
                valid_image_count += 1
                scores.append(r["iptc_score"])
                if r.get("has_c2pa"):
                    c2pa_count += 1
                    bucket = _classify_c2pa(
                        r.get("c2pa_validation_status"),
                        list(r.get("c2pa_failure_codes") or []),
                    )
                    c2pa_outcome_counts[bucket] += 1
        history.append({
            "run_id": run["run_id"],
            "started_at": run_started,
            "site_count": run["site_count_succeeded"],
            "image_count": run["image_count"],
            "mean_score": round(mean(scores), 2) if scores else 0.0,
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

    (out_dir / "history.json").write_text(json.dumps(history, indent=2))
    (out_dir / "history_by_site.json").write_text(
        json.dumps(dict(history_by_site), indent=2)
    )
    (out_dir / "history_by_country.json").write_text(
        json.dumps(dict(history_by_country), indent=2)
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
        runs_index.append({
            "run_id": rmeta["run_id"],
            "started_at": fmt_dt(rmeta["started_at"]),
            "ended_at": fmt_dt(rmeta["ended_at"]),
            "site_count_attempted": rmeta["site_count_attempted"],
            "site_count_succeeded": rmeta["site_count_succeeded"],
            "image_count": rmeta["image_count"],
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
