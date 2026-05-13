"""CLI entry point for the Metawatch crawler."""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime
from pathlib import Path

import click
import httpx
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from . import DEFAULT_HEADERS, __version__, config, discovery
from .articles import fetch_article
from .images import ExifPool, fetch_and_analyse
from .output import (
    ArticleRow,
    ImageRow,
    MetadataFieldRow,
    RunOutput,
    RunRow,
    SiteRow,
    write_run,
)
from .scoring import FIELD_WEIGHTS

console = Console()

DEFAULT_REQ_DELAY = 1.0
DEFAULT_REQ_JITTER = 0.5
SITE_TIMEOUT_SECONDS = 300  # 5-min hard cap per site so one stuck site can't stall the whole run


@click.group()
@click.version_option(__version__)
def cli() -> None:
    """Metawatch crawler."""


@cli.command()
@click.option("--output", "output_dir", required=True, type=click.Path(path_type=Path))
@click.option("--country", default=None, help="ISO alpha-2; restrict to one country.")
@click.option("--site-id", default=None, help="Run only these site IDs (comma-separated).")
@click.option("--publishers-dir", default=None, type=click.Path(path_type=Path),
              help="Override publishers config directory (defaults to ../config/publishers).")
@click.option("--concurrency", default=8, show_default=True)
def run(
    output_dir: Path,
    country: str | None,
    site_id: str | None,
    publishers_dir: Path | None,
    concurrency: int,
) -> None:
    """Execute a crawl run and write Parquet output."""
    publishers_path = publishers_dir or _default_publishers_dir()
    sites = config.load_sites(publishers_path, country=country)
    if site_id:
        wanted = {s.strip() for s in site_id.split(",") if s.strip()}
        sites = [s for s in sites if s.id in wanted]
    if not sites:
        console.print("[red]No sites selected.[/red]")
        raise SystemExit(2)

    optouts = config.load_optouts(publishers_path)
    sites = [s for s in sites if _domain_of(s.url) not in optouts]

    console.print(f"[bold]Metawatch crawler[/bold] v{__version__}")
    console.print(f"Selected {len(sites)} sites, output → {output_dir}")

    asyncio.run(_run_async(sites, output_dir, concurrency))


@cli.command("smoke-test")
@click.option("--site-id", required=True)
@click.option("--publishers-dir", default=None, type=click.Path(path_type=Path))
def smoke_test(site_id: str, publishers_dir: Path | None) -> None:
    """Single-site dry run; prints what would be crawled. No Parquet written."""
    publishers_path = publishers_dir or _default_publishers_dir()
    sites = config.load_sites(publishers_path)
    match = [s for s in sites if s.id == site_id]
    if not match:
        console.print(f"[red]Site '{site_id}' not found.[/red]")
        raise SystemExit(2)
    asyncio.run(_smoke_async(match[0]))


def _default_publishers_dir() -> Path:
    here = Path(__file__).resolve()
    return here.parents[3] / "config" / "publishers"


def _domain_of(url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(url).hostname or "").lower()


async def _smoke_async(site: config.Site) -> None:
    async with httpx.AsyncClient(headers=DEFAULT_HEADERS) as client:
        robots, sitemap_url, strategy, articles = await discovery.discover(client, site)
    console.print(f"[bold]{site.name}[/bold] ({site.id}) — {site.country}")
    console.print(f"  robots.txt fetched: {robots.fetched}  allowed: {robots.allowed_at_root}")
    console.print(f"  robots sitemaps: {len(robots.sitemap_urls)}  chosen: {sitemap_url}  strategy: {strategy}")
    console.print(f"  candidate articles: {len(articles)}")
    for a in articles[:5]:
        console.print(f"    - {a.url}  ({a.publication_date})")


async def _run_async(sites: list[config.Site], output_dir: Path, concurrency: int) -> None:
    run_id = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    started = datetime.now(UTC)

    site_rows: list[SiteRow] = []
    article_rows: list[ArticleRow] = []
    image_rows: list[ImageRow] = []
    field_rows: list[MetadataFieldRow] = []

    succeeded = 0
    blocked = 0

    timeout = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
    limits = httpx.Limits(max_connections=concurrency * 4, max_keepalive_connections=concurrency)

    async with (
        httpx.AsyncClient(
            headers={**DEFAULT_HEADERS, "Accept-Encoding": "gzip"},
            timeout=timeout,
            limits=limits,
            follow_redirects=True,
        ) as client,
        ExifPool() as exif_pool,
    ):
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("crawling", total=len(sites))

            domain_locks: dict[str, asyncio.Lock] = {}

            async def crawl_one(site: config.Site) -> None:
                nonlocal succeeded, blocked
                console.print(f"[dim]start {site.id}[/dim]")
                try:
                    site_result = await asyncio.wait_for(
                        _crawl_site(client, exif_pool, site, run_id, domain_locks),
                        timeout=SITE_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    console.print(f"[red]timeout[/red] {site.id}: exceeded {SITE_TIMEOUT_SECONDS}s")
                    site_rows.append(
                        SiteRow(
                            run_id=run_id, site_id=site.id, site_name=site.name,
                            country=site.country, category=site.category,
                            status="timeout", robots_url=f"{site.url.rstrip('/')}/robots.txt",
                            sitemap_url_used=None, discovery_strategy="timeout",
                            articles_sampled=0, images_analysed=0, mean_iptc_score=0.0,
                        )
                    )
                    return
                except Exception as e:  # network errors etc. — record and move on
                    console.print(f"[yellow]error[/yellow] {site.id}: {e}")
                    site_rows.append(
                        SiteRow(
                            run_id=run_id, site_id=site.id, site_name=site.name,
                            country=site.country, category=site.category,
                            status="error", robots_url=f"{site.url.rstrip('/')}/robots.txt",
                            sitemap_url_used=None, discovery_strategy="error",
                            articles_sampled=0, images_analysed=0, mean_iptc_score=0.0,
                        )
                    )
                    return

                site_rows.append(site_result["site"])
                article_rows.extend(site_result["articles"])
                image_rows.extend(site_result["images"])
                field_rows.extend(site_result["fields"])

                row = site_result["site"]
                if row.status == "robots_disallow":
                    blocked += 1
                    console.print(f"[cyan]blocked[/cyan] {site.id}: robots disallow")
                elif row.status == "ok":
                    succeeded += 1
                    console.print(
                        f"[green]ok[/green] {site.id}: "
                        f"{row.articles_sampled} articles, "
                        f"{row.images_analysed} images, "
                        f"mean score {row.mean_iptc_score:.1f}"
                    )
                else:
                    console.print(f"[magenta]{row.status}[/magenta] {site.id}")

            sem = asyncio.Semaphore(concurrency)

            async def with_sem(site: config.Site) -> None:
                async with sem:
                    await crawl_one(site)
                    progress.advance(task)

            await asyncio.gather(*(with_sem(s) for s in sites))

    ended = datetime.now(UTC)
    run = RunRow(
        run_id=run_id, started_at=started, ended_at=ended,
        crawler_version=__version__,
        site_count_attempted=len(sites),
        site_count_succeeded=succeeded,
        site_count_robots_blocked=blocked,
        article_count=len(article_rows),
        image_count=len(image_rows),
    )

    write_run(output_dir, RunOutput(run, site_rows, article_rows, image_rows, field_rows))
    console.print(
        f"[green]Done.[/green] {len(sites)} sites, {len(article_rows)} articles, "
        f"{len(image_rows)} images. Output: {output_dir}"
    )


async def _crawl_site(
    client: httpx.AsyncClient,
    exif_pool: ExifPool,
    site: config.Site,
    run_id: str,
    domain_locks: dict[str, asyncio.Lock],
) -> dict:
    robots, sitemap_url, strategy, candidates = await discovery.discover(client, site)
    robots_url = f"{site.url.rstrip('/')}/robots.txt"

    if not robots.allowed_at_root:
        return {
            "site": SiteRow(
                run_id=run_id, site_id=site.id, site_name=site.name,
                country=site.country, category=site.category,
                status="robots_disallow", robots_url=robots_url,
                sitemap_url_used=None, discovery_strategy=strategy,
                articles_sampled=0, images_analysed=0, mean_iptc_score=0.0,
            ),
            "articles": [], "images": [], "fields": [],
        }

    if not candidates:
        return {
            "site": SiteRow(
                run_id=run_id, site_id=site.id, site_name=site.name,
                country=site.country, category=site.category,
                status="no_articles_found", robots_url=robots_url,
                sitemap_url_used=sitemap_url, discovery_strategy=strategy,
                articles_sampled=0, images_analysed=0, mean_iptc_score=0.0,
            ),
            "articles": [], "images": [], "fields": [],
        }

    domain = _domain_of(site.url)
    lock = domain_locks.setdefault(domain, asyncio.Lock())
    delay = max(robots.crawl_delay or 0.0, DEFAULT_REQ_DELAY)

    articles: list[ArticleRow] = []
    images: list[ImageRow] = []
    fields: list[MetadataFieldRow] = []
    image_scores: list[float] = []
    seen_image_urls: set[str] = set()

    for cand in candidates:
        async with lock:
            await asyncio.sleep(delay + random.uniform(0, DEFAULT_REQ_JITTER))
            art = await fetch_article(client, cand)

        articles.append(
            ArticleRow(
                run_id=run_id, site_id=site.id, article_url=cand.url,
                publication_date=cand.publication_date, title=cand.title,
                language=cand.language, keywords=cand.keywords,
                jsonld_news_article=art.jsonld_news_article,
                http_status=art.http_status, fetched_at=datetime.now(UTC),
            )
        )

        for img_url in art.image_urls:
            if img_url in seen_image_urls:
                continue
            seen_image_urls.add(img_url)
            async with lock:
                await asyncio.sleep(delay + random.uniform(0, DEFAULT_REQ_JITTER))
                img = await fetch_and_analyse(client, img_url, exif_pool)

            image_row = ImageRow(
                run_id=run_id, site_id=site.id,
                article_url_hash=_sha1_of(cand.url),
                image_url=img.image_url, mime_type=img.mime_type,
                width=img.width, height=img.height,
                file_size_bytes=img.file_size_bytes, http_status=img.http_status,
                has_exif=img.has_exif, has_iptc_iim=img.has_iptc_iim,
                has_iptc_xmp=img.has_iptc_xmp, has_c2pa=img.has_c2pa,
                c2pa_manifest_signer=img.c2pa_manifest_signer,
                c2pa_validation_status=img.c2pa_validation_status,
                cdn_provider=img.cdn_provider,
                cdn_optimizer_active=img.cdn_optimizer_active,
                metadata_field_count=img.metadata_field_count,
                iptc_score=img.iptc_score,
            )
            images.append(image_row)
            if img.http_status == 200:
                image_scores.append(img.iptc_score)

            for label, present in img.per_field_presence:
                family = _family_for(label)
                fields.append(
                    MetadataFieldRow(
                        run_id=run_id, image_url_hash=_sha1_of(img.image_url),
                        family=family, field_name=label, has_value=present,
                    )
                )

    mean_score = round(sum(image_scores) / len(image_scores), 2) if image_scores else 0.0

    return {
        "site": SiteRow(
            run_id=run_id, site_id=site.id, site_name=site.name,
            country=site.country, category=site.category,
            status="ok", robots_url=robots_url,
            sitemap_url_used=sitemap_url, discovery_strategy=strategy,
            articles_sampled=len(articles), images_analysed=len(images),
            mean_iptc_score=mean_score,
        ),
        "articles": articles,
        "images": images,
        "fields": fields,
    }


def _family_for(label: str) -> str:
    return "IPTC"  # all current scored fields live in IPTC/XMP; treat as IPTC for the long table


def _sha1_of(s: str) -> str:
    import hashlib
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


# expose FIELD_WEIGHTS so the about page generator can introspect them later
__all__ = ["cli", "FIELD_WEIGHTS"]
