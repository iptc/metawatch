"""Convert v1's feeds_list.csv into per-country YAML files.

One-shot import. After running, edit individual config/publishers/*.yaml to clean up.

Usage:
    python scripts/import_v1_list.py path/to/feeds_list.csv path/to/config/publishers/
"""

from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import yaml


def slugify(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "site"


NEWS_AGENCY_NAMES = {
    "associated press", "ap news", "reuters", "agence france-presse", "afp",
    "press association", "pa media", "kyodo", "dpa", "ansa", "efe", "bloomberg",
    "tass", "xinhua", "anadolu", "yonhap",
}


def guess_category(name: str) -> str:
    n = name.lower()
    if any(w in n for w in NEWS_AGENCY_NAMES):
        return "news-agency"
    if any(w in n for w in ["bbc", "cnn", "abc news", "nbc", "cbs", "rte", "rai", "ard", "zdf", "tv "]):
        return "broadcaster"
    return "newspaper"


def main(csv_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[dict]] = defaultdict(list)
    seen_ids: set[tuple[str, str]] = set()  # (country, id)

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cc = (row.get("Country") or "").strip().upper()
            name = (row.get("Site Name") or "").strip()
            site_url = (row.get("Site URL") or "").strip()
            feed_url = (row.get("Feed URL") or "").strip()
            suggested = (row.get("SuggestedBy") or "").strip()
            if not cc or not name or not site_url:
                continue
            if not site_url.startswith(("http://", "https://")):
                site_url = "https://" + site_url
            host = urlparse(site_url).hostname or ""
            if not host:
                continue

            site_id_base = slugify(name)
            site_id = site_id_base
            i = 2
            while (cc, site_id) in seen_ids:
                site_id = f"{site_id_base}-{i}"
                i += 1
            seen_ids.add((cc, site_id))

            entry = {
                "id": site_id,
                "name": name,
                "url": site_url,
                "homepage": site_url,
                "category": guess_category(name),
                "language_primary": "en",
                "notes": "",
                "suggested_by": suggested or "original-2021-list",
                "active": True,
                "discovery": {
                    "strategy": "sitemap",
                    "sitemap_urls": [],
                    "picture_sitemap_url": None,
                    "rss_urls": [feed_url] if feed_url else [],
                    "js_required": False,
                },
                "sample": {"max_articles": 20, "window_days": 30},
            }
            grouped[cc].append(entry)

    for cc in sorted(grouped):
        path = out_dir / f"{cc.lower()}.yaml"
        sites = sorted(grouped[cc], key=lambda x: x["id"])
        data = {"country": cc, "sites": sites}
        with path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, width=120)
        print(f"  {cc}: {len(sites)} sites → {path}")

    total = sum(len(v) for v in grouped.values())
    print(f"\nWrote {total} sites across {len(grouped)} countries.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: import_v1_list.py <csv_path> <out_dir>")
        sys.exit(2)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
