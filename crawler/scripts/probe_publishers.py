"""Lightweight probe of every publisher YAML's URLs.

Validates that homepage / RSS / sitemap URLs from config/publishers/*.yaml
still resolve and that RSS feeds still parse. Does NOT fetch articles or
images — that's the job of the full crawler.

Usage:
    python crawler/scripts/probe_publishers.py
    python crawler/scripts/probe_publishers.py --country GB
    python crawler/scripts/probe_publishers.py --out data/probes/2026-05-12.json

Output: one JSON file with per-site results, plus a markdown summary on stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import httpx
import yaml

from pmd_crawler import DEFAULT_HEADERS

TIMEOUT = 15.0
CONCURRENCY = 16


def _host(url: str | None) -> str:
    if not url:
        return ""
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


@dataclass
class UrlProbe:
    url: str
    final_url: str | None = None
    status: int | None = None
    error: str | None = None
    redirected: bool = False
    host_changed: bool = False
    rss_entries: int | None = None  # only set for RSS probes


@dataclass
class SiteProbe:
    country: str
    id: str
    name: str
    homepage: UrlProbe
    rss: list[UrlProbe] = field(default_factory=list)
    sitemap: list[UrlProbe] = field(default_factory=list)

    @property
    def health(self) -> str:
        """One-word status for the summary."""
        if not self._url_ok(self.homepage):
            return "homepage_fail"
        # If a discovery strategy is configured, at least one of its URLs should work.
        has_rss = bool(self.rss)
        has_sitemap = bool(self.sitemap)
        if has_rss and not any(self._rss_ok(p) for p in self.rss):
            if has_sitemap and any(self._url_ok(p) for p in self.sitemap):
                return "rss_dead_sitemap_ok"
            return "rss_dead"
        if has_sitemap and not any(self._url_ok(p) for p in self.sitemap):
            if has_rss and any(self._rss_ok(p) for p in self.rss):
                return "sitemap_dead_rss_ok"
            return "sitemap_dead"
        if self.homepage.host_changed:
            return "host_changed"
        return "ok"

    @staticmethod
    def _url_ok(p: UrlProbe) -> bool:
        return p.status is not None and 200 <= p.status < 400

    @staticmethod
    def _rss_ok(p: UrlProbe) -> bool:
        return SiteProbe._url_ok(p) and (p.rss_entries or 0) > 0


async def probe_url(client: httpx.AsyncClient, url: str, *, parse_rss: bool = False) -> UrlProbe:
    out = UrlProbe(url=url)
    try:
        # GET (not HEAD) — many sites 405/403 HEAD or return wrong status
        r = await client.get(url, follow_redirects=True)
        out.status = r.status_code
        out.final_url = str(r.url)
        out.redirected = str(r.url).rstrip("/") != url.rstrip("/")
        out.host_changed = _host(str(r.url)) != _host(url)
        if parse_rss and r.status_code < 400:
            parsed = feedparser.parse(r.content)
            out.rss_entries = len(parsed.entries) if not parsed.bozo or parsed.entries else 0
            if parsed.bozo and not parsed.entries:
                out.error = f"rss_parse: {parsed.bozo_exception!r}"[:200]
    except httpx.HTTPError as e:
        out.error = f"{type(e).__name__}: {e}"[:200]
    except Exception as e:  # noqa: BLE001 - network can throw anything
        out.error = f"{type(e).__name__}: {e}"[:200]
    return out


async def probe_site(sem: asyncio.Semaphore, client: httpx.AsyncClient, country: str, site: dict) -> SiteProbe:
    async with sem:
        homepage_url = site.get("homepage") or site.get("url")
        homepage = await probe_url(client, homepage_url)

        disc = site.get("discovery") or {}
        rss_urls = disc.get("rss_urls") or []
        sitemap_urls = disc.get("sitemap_urls") or []

        rss_results = await asyncio.gather(*[probe_url(client, u, parse_rss=True) for u in rss_urls])
        sitemap_results = await asyncio.gather(*[probe_url(client, u) for u in sitemap_urls])

        return SiteProbe(
            country=country,
            id=site["id"],
            name=site["name"],
            homepage=homepage,
            rss=list(rss_results),
            sitemap=list(sitemap_results),
        )


async def probe_all(publisher_dir: Path, country_filter: str | None) -> list[SiteProbe]:
    sem = asyncio.Semaphore(CONCURRENCY)
    headers = {**DEFAULT_HEADERS, "Accept": "*/*"}
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers) as client:
        tasks = []
        for p in sorted(publisher_dir.glob("*.yaml")):
            if p.name.startswith("_"):
                continue
            data = yaml.safe_load(p.read_text())
            country = data["country"]
            if country_filter and country != country_filter.upper():
                continue
            for site in data.get("sites", []):
                tasks.append(probe_site(sem, client, country, site))
        return await asyncio.gather(*tasks)


def _to_dict(probe: SiteProbe) -> dict:
    d = asdict(probe)
    d["health"] = probe.health
    return d


def print_summary(probes: list[SiteProbe]) -> None:
    from collections import Counter

    by_health = Counter(p.health for p in probes)
    print(f"\n## Probe summary — {len(probes)} sites\n")
    for k, v in by_health.most_common():
        print(f"- **{k}**: {v}")

    bad = [p for p in probes if p.health != "ok"]
    if bad:
        print(f"\n## {len(bad)} sites needing attention\n")
        print("| Country | ID | Health | Homepage status | Notes |")
        print("|---|---|---|---|---|")
        for p in sorted(bad, key=lambda x: (x.health, x.country, x.id)):
            notes = []
            if p.homepage.error:
                notes.append(f"home err: {p.homepage.error[:60]}")
            if p.homepage.redirected:
                notes.append(f"→ {p.homepage.final_url}")
            dead_rss = [r.url for r in p.rss if not SiteProbe._rss_ok(r)]
            if dead_rss:
                notes.append(f"{len(dead_rss)}/{len(p.rss)} RSS dead")
            dead_sm = [s.url for s in p.sitemap if not SiteProbe._url_ok(s)]
            if dead_sm:
                notes.append(f"{len(dead_sm)}/{len(p.sitemap)} sitemap dead")
            note = "; ".join(notes) or "—"
            print(f"| {p.country} | {p.id} | {p.health} | {p.homepage.status} | {note} |")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--publishers", default="config/publishers", help="Publisher YAML directory")
    ap.add_argument("--country", help="Restrict to a single country (alpha-2)")
    ap.add_argument("--out", help="Write detailed JSON results to this path")
    args = ap.parse_args()

    publisher_dir = Path(args.publishers)
    probes = asyncio.run(probe_all(publisher_dir, args.country))

    out_path = Path(args.out) if args.out else Path("data/probes") / f"{datetime.now(UTC).strftime('%Y-%m-%d')}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps([_to_dict(p) for p in probes], indent=2))
    print(f"Wrote {len(probes)} probe results to {out_path}")
    print_summary(probes)


if __name__ == "__main__":
    main()
