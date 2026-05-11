"""Load and validate the per-country site YAML files."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class DiscoveryConfig:
    strategy: str = "sitemap"  # sitemap | rss | homepage
    sitemap_urls: list[str] = field(default_factory=list)
    picture_sitemap_url: str | None = None
    rss_urls: list[str] = field(default_factory=list)
    js_required: bool = False


@dataclass
class SampleConfig:
    max_articles: int = 20
    window_days: int = 30


@dataclass
class Site:
    id: str
    name: str
    url: str
    country: str
    homepage: str
    discovery: DiscoveryConfig
    sample: SampleConfig
    category: str = "newspaper"
    language_primary: str = "en"
    notes: str = ""
    suggested_by: str = ""
    active: bool = True


def load_sites(publishers_dir: Path, country: str | None = None) -> list[Site]:
    """Load every active site from config/publishers/*.yaml.

    If `country` is given (alpha-2, case-insensitive), only that country is loaded.
    """
    sites: list[Site] = []
    target = country.lower() if country else None
    for path in sorted(publishers_dir.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        if target and path.stem != target:
            continue
        data = yaml.safe_load(path.read_text())
        if not data:
            continue
        cc = data["country"]
        for entry in data.get("sites", []):
            if not entry.get("active", True):
                continue
            disc_raw = entry.get("discovery", {})
            samp_raw = entry.get("sample", {})
            sites.append(
                Site(
                    id=entry["id"],
                    name=entry["name"],
                    url=entry["url"],
                    country=cc.upper(),
                    homepage=entry.get("homepage", entry["url"]),
                    discovery=DiscoveryConfig(
                        strategy=disc_raw.get("strategy", "sitemap"),
                        sitemap_urls=disc_raw.get("sitemap_urls") or [],
                        picture_sitemap_url=disc_raw.get("picture_sitemap_url"),
                        rss_urls=disc_raw.get("rss_urls") or [],
                        js_required=disc_raw.get("js_required", False),
                    ),
                    sample=SampleConfig(
                        max_articles=samp_raw.get("max_articles", 20),
                        window_days=samp_raw.get("window_days", 30),
                    ),
                    category=entry.get("category", "newspaper"),
                    language_primary=entry.get("language_primary", "en"),
                    notes=entry.get("notes", "") or "",
                    suggested_by=entry.get("suggested_by", "") or "",
                    active=entry.get("active", True),
                )
            )
    return sites


def load_optouts(publishers_dir: Path) -> set[str]:
    """Return the set of domain stems we've been asked not to crawl."""
    path = publishers_dir / "_optouts.yaml"
    if not path.exists():
        return set()
    data = yaml.safe_load(path.read_text()) or {}
    return set(data.get("domains", []))
