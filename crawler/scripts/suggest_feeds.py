"""Find replacement RSS feeds or sitemaps for sites whose configured RSS is dead.

For each site in the latest probe JSON whose health is `rss_dead` (homepage
works, RSS doesn't), this script:

  1. Fetches the homepage and extracts every <link rel="alternate"
     type="application/rss+xml"> (or atom+xml) candidate.
  2. Probes a small set of common sitemap paths plus any sitemaps declared
     in robots.txt.
  3. Verifies each candidate parses (feedparser for RSS, XML root check for
     sitemap) and reports results.

Output: data/probes/<date>-feed-suggestions.json plus a markdown summary.

Usage:
    python crawler/scripts/suggest_feeds.py --probe data/probes/2026-05-12.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
import yaml
from selectolax.parser import HTMLParser

from pmd_crawler import USER_AGENT

TIMEOUT = 20.0
CONCURRENCY = 8
SITEMAP_PATHS = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/news-sitemap.xml",
    "/sitemap-news.xml",
    "/sitemaps.xml",
]


@dataclass
class Candidate:
    url: str
    kind: str  # "rss" | "sitemap"
    source: str  # "html-link" | "common-path" | "robots-txt"
    status: int | None = None
    valid: bool = False
    entries: int | None = None
    error: str | None = None


@dataclass
class Suggestion:
    country: str
    id: str
    name: str
    homepage: str
    rss: list[Candidate] = field(default_factory=list)
    sitemap: list[Candidate] = field(default_factory=list)

    @property
    def best_rss(self) -> Candidate | None:
        return next((c for c in self.rss if c.valid), None)

    @property
    def best_sitemap(self) -> Candidate | None:
        return next((c for c in self.sitemap if c.valid), None)


async def discover_rss_links(client: httpx.AsyncClient, homepage: str) -> list[str]:
    try:
        r = await client.get(homepage, follow_redirects=True)
        if r.status_code >= 400:
            return []
        tree = HTMLParser(r.text)
        out: list[str] = []
        for node in tree.css('link[rel="alternate"]'):
            t = (node.attributes.get("type") or "").lower()
            href = node.attributes.get("href")
            if href and ("rss" in t or "atom" in t):
                out.append(urljoin(str(r.url), href))
        # Dedupe preserving order
        seen = set()
        return [u for u in out if not (u in seen or seen.add(u))]
    except Exception:  # noqa: BLE001
        return []


async def discover_robots_sitemaps(client: httpx.AsyncClient, homepage: str) -> list[str]:
    try:
        parsed = urlparse(homepage)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        r = await client.get(robots_url, follow_redirects=True)
        if r.status_code >= 400:
            return []
        sitemaps = []
        for line in r.text.splitlines():
            if line.lower().startswith("sitemap:"):
                sitemaps.append(line.split(":", 1)[1].strip())
        return sitemaps
    except Exception:  # noqa: BLE001
        return []


async def verify_rss(client: httpx.AsyncClient, url: str) -> tuple[int | None, bool, int | None, str | None]:
    try:
        r = await client.get(url, follow_redirects=True)
        if r.status_code >= 400:
            return r.status_code, False, None, None
        parsed = feedparser.parse(r.content)
        entries = len(parsed.entries)
        valid = entries > 0
        err = None if valid else (f"{parsed.bozo_exception!r}"[:120] if parsed.bozo else "no entries")
        return r.status_code, valid, entries, err
    except httpx.HTTPError as e:
        return None, False, None, f"{type(e).__name__}: {e}"[:120]


async def verify_sitemap(client: httpx.AsyncClient, url: str) -> tuple[int | None, bool, str | None]:
    try:
        r = await client.get(url, follow_redirects=True)
        if r.status_code >= 400:
            return r.status_code, False, None
        body = r.text.lstrip()[:512].lower()
        valid = "<urlset" in body or "<sitemapindex" in body
        return r.status_code, valid, None if valid else "not a sitemap root"
    except httpx.HTTPError as e:
        return None, False, f"{type(e).__name__}: {e}"[:120]


async def suggest_for_site(sem: asyncio.Semaphore, client: httpx.AsyncClient, country: str, site: dict, homepage: str) -> Suggestion:
    async with sem:
        suggestion = Suggestion(country=country, id=site["id"], name=site["name"], homepage=homepage)

        rss_links = await discover_rss_links(client, homepage)
        robots_sitemaps = await discover_robots_sitemaps(client, homepage)

        rss_cands = [Candidate(u, "rss", "html-link") for u in rss_links]
        sm_cands: list[Candidate] = []
        sm_cands.extend(Candidate(u, "sitemap", "robots-txt") for u in robots_sitemaps)
        for p in SITEMAP_PATHS:
            sm_cands.append(Candidate(urljoin(homepage, p), "sitemap", "common-path"))

        # Dedupe sitemap candidates by URL
        seen: set[str] = set()
        sm_cands = [c for c in sm_cands if not (c.url in seen or seen.add(c.url))]

        for c in rss_cands:
            c.status, c.valid, c.entries, c.error = await verify_rss(client, c.url)
        for c in sm_cands:
            c.status, c.valid, c.error = await verify_sitemap(client, c.url)

        suggestion.rss = rss_cands
        suggestion.sitemap = sm_cands
        return suggestion


def _to_dict(s: Suggestion) -> dict:
    d = asdict(s)
    d["best_rss"] = s.best_rss.url if s.best_rss else None
    d["best_sitemap"] = s.best_sitemap.url if s.best_sitemap else None
    return d


def load_targets(probe_path: Path, publishers_dir: Path) -> list[tuple[str, dict, str]]:
    """Return (country, site_dict, homepage_url) for every rss_dead site."""
    probe = json.load(open(probe_path))
    rss_dead = {(s["country"], s["id"]): s for s in probe if s["health"] == "rss_dead"}
    targets: list[tuple[str, dict, str]] = []
    for p in sorted(publishers_dir.glob("*.yaml")):
        if p.name.startswith("_"):
            continue
        data = yaml.safe_load(p.read_text())
        country = data["country"]
        for site in data.get("sites", []):
            key = (country, site["id"])
            if key not in rss_dead:
                continue
            probe_site = rss_dead[key]
            homepage = probe_site["homepage"]["final_url"] or probe_site["homepage"]["url"]
            targets.append((country, site, homepage))
    return targets


async def run(targets: list[tuple[str, dict, str]]) -> list[Suggestion]:
    sem = asyncio.Semaphore(CONCURRENCY)
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers) as client:
        return await asyncio.gather(*[suggest_for_site(sem, client, c, s, h) for c, s, h in targets])


def print_summary(results: list[Suggestion]) -> None:
    rss_only = [s for s in results if s.best_rss and not s.best_sitemap]
    sm_only = [s for s in results if s.best_sitemap and not s.best_rss]
    both = [s for s in results if s.best_rss and s.best_sitemap]
    neither = [s for s in results if not s.best_rss and not s.best_sitemap]
    print(f"\n## Feed-discovery summary — {len(results)} sites\n")
    print(f"- both RSS and sitemap found: **{len(both)}**")
    print(f"- RSS only: **{len(rss_only)}**")
    print(f"- sitemap only: **{len(sm_only)}**")
    print(f"- neither: **{len(neither)}**")

    if neither:
        print("\n### Manual triage needed\n")
        for s in neither:
            print(f"- {s.country}/{s.id} ({s.homepage})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", required=True, help="Probe JSON to read rss_dead targets from")
    ap.add_argument("--publishers", default="config/publishers")
    ap.add_argument("--out")
    args = ap.parse_args()

    targets = load_targets(Path(args.probe), Path(args.publishers))
    print(f"Probing {len(targets)} sites for feed/sitemap candidates...")
    results = asyncio.run(run(targets))

    out_path = Path(args.out) if args.out else Path("data/probes") / f"{datetime.now(UTC).strftime('%Y-%m-%d')}-feed-suggestions.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps([_to_dict(s) for s in results], indent=2))
    print(f"Wrote {len(results)} suggestions to {out_path}")
    print_summary(results)


if __name__ == "__main__":
    main()
