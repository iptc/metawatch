"""Tests C2PA top sites ranking correctly use detection rates."""

import importlib.util
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from pmd_crawler.output import ImageRow, SiteRow

_spec = importlib.util.spec_from_file_location(
    "_export_for_site",
    Path(__file__).resolve().parents[1] / "scripts" / "export_for_site.py",
)
assert _spec is not None, "Could not create a module spec for export_for_site.py"
assert _spec.loader is not None, "No module loader available for export_for_site.py"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


@pytest.mark.parametrize(
    ("samples", "expected"),
    [
        ([("large", 10, 4), ("small", 2, 1), ("none", 3, 0)], ["small", "large"]),
        ([("lower", 201, 1), ("higher", 200, 1)], ["higher", "lower"]),
        ([("beta", 2, 1), ("alpha", 2, 1), ("large", 4, 2)], ["large", "alpha", "beta"]),
        ([("none", 3, 0), ("empty", 0, 0)], []),
        ([], []),
    ],
)
def test_top_sites_ranked_by_percentage(tmp_path, monkeypatch, samples, expected):
    sites = []
    images = []
    for sid, total, detected in samples:
        sites.append(asdict(SiteRow(
            run_id="test", site_id=sid, site_name=sid.title(), country="GB",
            category="newspaper", status="ok", robots_url="",
            sitemap_url_used=None, discovery_strategy="config:rss",
            articles_sampled=0, images_analysed=total, mean_iptc_score=0.0,
        )))
        for index in range(total):
            image = ImageRow(
                run_id="test", site_id=sid, article_url_hash="article",
                image_url=f"https://example.org/{sid}/{index}.jpg",
                mime_type="image/jpeg", width=None, height=None,
                file_size_bytes=None, http_status=200,
                has_exif=False, has_iptc_iim=False, has_iptc_xmp=False,
                has_c2pa=index < detected, c2pa_manifest_signer=None,
                c2pa_validation_status=None, cdn_provider="unknown",
                cdn_optimizer_active="unknown", metadata_field_count=0,
                iptc_score=0.0,
            )
            images.append({**asdict(image), "image_url_hash": image.image_url_hash})

    tables = {"sites.parquet": sites, "images.parquet": images}
    monkeypatch.setattr(_mod, "read", lambda path: tables.get(path.name, []))
    _mod.export_run(tmp_path / "run", tmp_path / "out", [])

    data = json.loads((tmp_path / "out" / "c2pa.json").read_text())
    assert [row["site_id"] for row in data["top_sites"]] == expected
    counts = {sid: (total, detected) for sid, total, detected in samples}
    for row in data["top_sites"]:
        total, detected = counts[row["site_id"]]
        assert row == {
            "site_id": row["site_id"],
            "site_name": row["site_id"].title(),
            "country": "GB",
            "images_with_c2pa": detected,
            "images_analysed": total,
            "pct_with_c2pa": round(100.0 * detected / total, 2),
        }
    assert data["image_count_total"] == len(images)
    assert data["image_count_with_c2pa"] == sum(detected for _, _, detected in samples)
