"""CLI entry point for the Metawatch crawler."""

from __future__ import annotations

import asyncio
import json
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
from .optout import SiteOptoutSignals, probe_site_optouts
from .output import (
    ArticleRow,
    ImageRow,
    MetadataFieldRow,
    RobotsAnalysisRow,
    RunOutput,
    RunRow,
    SiteRow,
    merge_run,
    write_run,
)
from .scoring import stored_evidence_tags

console = Console()

DEFAULT_REQ_DELAY = 1.0
DEFAULT_REQ_JITTER = 0.5
SITE_TIMEOUT_SECONDS = 300  # 5-min hard cap per site so one stuck site can't stall the whole run
MAX_CONSECUTIVE_ARTICLE_FAILURES = 3  # Bail out of a site after N back-to-back article fetch failures


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
@click.option(
    "--merge", "merge", is_flag=True, default=False,
    help="Merge into an existing run directory instead of overwriting. "
         "Use with --site-id or --country to patch just the affected publishers "
         "into today's full crawl without losing the rest.",
)
def run(
    output_dir: Path,
    country: str | None,
    site_id: str | None,
    publishers_dir: Path | None,
    concurrency: int,
    merge: bool,
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
    mode = "merge into" if merge else "write to"
    console.print(f"Selected {len(sites)} sites, {mode} → {output_dir}")

    asyncio.run(_run_async(sites, output_dir, concurrency, merge=merge))


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
        robots, sitemap_url, strategy, articles, _disc_err = await discovery.discover(client, site)
    console.print(f"[bold]{site.name}[/bold] ({site.id}) — {site.country}")
    console.print(f"  robots.txt fetched: {robots.fetched}  allowed: {robots.allowed_at_root}")
    console.print(f"  robots sitemaps: {len(robots.sitemap_urls)}  chosen: {sitemap_url}  strategy: {strategy}")
    console.print(f"  candidate articles: {len(articles)}")
    for a in articles[:5]:
        console.print(f"    - {a.url}  ({a.publication_date})")


async def _run_async(
    sites: list[config.Site], output_dir: Path, concurrency: int, *, merge: bool = False,
) -> None:
    run_id = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    started = datetime.now(UTC)

    site_rows: list[SiteRow] = []
    article_rows: list[ArticleRow] = []
    image_rows: list[ImageRow] = []
    field_rows: list[MetadataFieldRow] = []
    robots_rows: list[RobotsAnalysisRow] = []

    succeeded = 0
    blocked = 0

    timeout = httpx.Timeout(connect=10.0, read=15.0, write=10.0, pool=10.0)
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
                # NOTE: timeout/error paths skip optout probes — we never got a
                # chance to fetch robots.txt cleanly. SiteRow.has_* stays at the
                # dataclass defaults (False/None/0); aggregations should exclude
                # these statuses from denominator. See export_for_site.

                site_rows.append(site_result["site"])
                article_rows.extend(site_result["articles"])
                image_rows.extend(site_result["images"])
                field_rows.extend(site_result["fields"])
                robots_rows.extend(site_result["robots_analysis"])

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
                elif row.status == "unreachable":
                    console.print(f"[red]unreachable[/red] {site.id}: all sources network-failed")
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

    output = RunOutput(run, site_rows, article_rows, image_rows, field_rows, robots_rows)
    if merge:
        merge_run(output_dir, output)
    else:
        write_run(output_dir, output)
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
    robots, sitemap_url, strategy, candidates, disc_err = await discovery.discover(client, site)
    robots_url = f"{site.url.rstrip('/')}/robots.txt"

    # AI-opt-out probes (robots-AI matrix, RSL, tdmrep, ai.txt, trust.txt).
    # We run these for every site we could reach in any meaningful way,
    # even ones that disallowed our crawler — the opt-out posture is still
    # interesting and the probes are cheap.
    #
    # Order matters: discover() fetches robots up-front for sitemap-path
    # sites (it needs the Sitemap: lines), but for RSS-path sites it skips
    # robots entirely to dodge cascade-blocking WAFs (e.g. tass.ru returns
    # 403 on /robots.txt and then locks the IP out of every subsequent
    # request for ~60s). In the RSS case we still want robots.txt for the
    # AI-prefs signals, so do a best-effort fetch AFTER the article loop —
    # by then we've already got everything else we need from the site,
    # so a residual WAF block costs us nothing.
    optout_signals: SiteOptoutSignals | None = None
    robots_analysis: list[RobotsAnalysisRow] = []

    async def _ensure_optouts() -> None:
        """Lazily probe AI-prefs, fetching robots.txt now if it wasn't earlier."""
        nonlocal optout_signals, robots_analysis
        if optout_signals is not None:
            return
        if robots.fetched:
            robots_text = robots.text
        else:
            # Deferred robots fetch. May 4xx / time out (e.g. WAF-blocked) —
            # treat any failure as empty text and let probe_site_optouts
            # compute permissive defaults from there.
            try:
                r = await client.get(robots_url, timeout=15.0, follow_redirects=True)
                robots_text = r.text if r.status_code < 400 else ""
            except Exception:
                robots_text = ""
        optout_signals = await probe_site_optouts(client, site.url, robots_text)
        robots_analysis = _build_robots_analysis_rows(run_id, site.id, optout_signals)

    # If discover() already fetched robots, compute optouts now — no benefit
    # to deferring, and it lets the early-return paths use the same closure
    # without per-path re-checks.
    if robots.fetched:
        await _ensure_optouts()

    def _site_row(
        status: str, sitemap: str | None, articles: int, images: int, score: float,
        block_vendor: str | None = None,
    ) -> SiteRow:
        assert optout_signals is not None, "_site_row called before _ensure_optouts()"
        return SiteRow(
            run_id=run_id, site_id=site.id, site_name=site.name,
            country=site.country, category=site.category,
            status=status, robots_url=robots_url,
            sitemap_url_used=sitemap, discovery_strategy=strategy,
            articles_sampled=articles, images_analysed=images, mean_iptc_score=score,
            has_tdmrep=optout_signals.has_tdmrep,
            has_ai_txt=optout_signals.has_ai_txt,
            has_rsl=optout_signals.has_rsl,
            rsl_license_urls=list(optout_signals.rsl_license_urls),
            has_trust_txt=optout_signals.has_trust_txt,
            trust_txt_datatraining=optout_signals.trust_txt_datatraining,
            ai_bots_blocked_count=optout_signals.robots_ai.blocked_count,
            has_content_signals=optout_signals.has_content_signals,
            content_signal_ai_train=optout_signals.content_signals.get("ai-train"),
            content_signal_ai_input=optout_signals.content_signals.get("ai-input"),
            content_signal_search=optout_signals.content_signals.get("search"),
            block_vendor=block_vendor,
        )

    def _vendor_from_disc_err(err: str | None) -> str | None:
        """Extract the WAF vendor from a disc_err of the form 'waf:<vendor>'."""
        if err and err.startswith("waf:"):
            return err[4:] or None
        return None

    if not robots.allowed_at_root:
        # robots.fetched is True here (RSS-path synthetic robots is always
        # allowed_at_root=True), so _ensure_optouts has already run.
        return {
            "site": _site_row("robots_disallow", None, 0, 0, 0.0),
            "articles": [], "images": [], "fields": [],
            "robots_analysis": robots_analysis,
        }

    if not candidates:
        status = discovery.status_for_empty_discovery(strategy, disc_err)
        await _ensure_optouts()
        return {
            "site": _site_row(status, sitemap_url, 0, 0, 0.0,
                              block_vendor=_vendor_from_disc_err(disc_err)),
            "articles": [], "images": [], "fields": [],
            "robots_analysis": robots_analysis,
        }

    domain = _domain_of(site.url)
    lock = domain_locks.setdefault(domain, asyncio.Lock())
    delay = max(robots.crawl_delay or 0.0, DEFAULT_REQ_DELAY)

    # Cap article count by what fits in the site budget given the declared
    # crawl-delay. Each article costs ~2× delay because we acquire the
    # domain lock (with its sleep) once for the article fetch and once
    # for the hero image fetch. Reserve a few seconds for discovery + write.
    per_article_budget = 2 * (max(delay, 1.0) + DEFAULT_REQ_JITTER)
    reserved = 30.0  # discovery + parquet write
    max_within_budget = max(1, int((SITE_TIMEOUT_SECONDS - reserved) // per_article_budget))
    if max_within_budget < len(candidates):
        console.print(
            f"[yellow]capping[/yellow] {site.id} to {max_within_budget} articles "
            f"(crawl-delay {delay:.0f}s × {len(candidates)} would exceed {SITE_TIMEOUT_SECONDS}s budget)"
        )
        candidates = candidates[:max_within_budget]

    articles: list[ArticleRow] = []
    images: list[ImageRow] = []
    fields: list[MetadataFieldRow] = []
    image_scores: list[float] = []
    seen_image_urls: set[str] = set()
    consecutive_failures = 0
    bailed_unreachable = False

    for cand in candidates:
        async with lock:
            await asyncio.sleep(delay + random.uniform(0, DEFAULT_REQ_JITTER))
            art = await fetch_article(client, cand)

        # Bail out if articles keep failing — usually a stuck CDN that
        # would otherwise burn the whole per-site time budget on timeouts.
        if art.http_status == 0:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_ARTICLE_FAILURES:
                console.print(
                    f"[red]bail[/red] {site.id}: {consecutive_failures} consecutive article failures"
                )
                bailed_unreachable = True
                break
        else:
            consecutive_failures = 0

        articles.append(
            ArticleRow(
                run_id=run_id, site_id=site.id, article_url=cand.url,
                publication_date=cand.publication_date,
                # Discovery's title (news:title / RSS entry title) wins; the
                # headline read from the article HTML fills the gap for sites
                # whose sitemap carries no <news:news> block.
                title=cand.title or art.title,
                language=cand.language, keywords=cand.keywords,
                jsonld_news_article=art.jsonld_news_article,
                http_status=art.http_status, fetched_at=datetime.now(UTC),
                tdm_reservation=art.tdm_reservation,
            )
        )

        for img_url in art.image_urls:
            if img_url in seen_image_urls:
                continue
            seen_image_urls.add(img_url)
            async with lock:
                await asyncio.sleep(delay + random.uniform(0, DEFAULT_REQ_JITTER))
                img = await fetch_and_analyse(client, img_url, exif_pool)

            kept_tags = stored_evidence_tags(img.raw_tags) if img.raw_tags else {}
            # Combine noai tokens seen on the article (X-Robots-Tag, <meta robots>)
            # with those seen on the image response itself. Either is a valid
            # opt-out signal for this image.
            merged_noai: list[str] = []
            for tok in (*art.noai_tokens, *img.noai_tokens):
                if tok not in merged_noai:
                    merged_noai.append(tok)
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
                c2pa_failure_codes=img.c2pa_failure_codes,
                dst_iptc=img.dst_iptc,
                dst_c2pa=img.dst_c2pa,
                cdn_provider=img.cdn_provider,
                cdn_optimizer_active=img.cdn_optimizer_active,
                metadata_field_count=img.metadata_field_count,
                iptc_score=img.iptc_score,
                iptc_xmp_tags_json=json.dumps(kept_tags, default=str, sort_keys=True) if kept_tags else None,
                noai_tokens=merged_noai,
                cawg_training_mining_json=(
                    json.dumps(img.cawg_training_mining, default=str, sort_keys=True)
                    if img.cawg_training_mining is not None else None
                ),
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

    # Make sure AI-prefs are computed before we exit. For sitemap-path sites
    # this is a no-op (already done up front); for RSS-path sites it's where
    # the deferred robots.txt fetch happens, deliberately AFTER the article
    # loop so any cascade-blocking WAFs can't disrupt the article fetches.
    await _ensure_optouts()

    if bailed_unreachable and not articles:
        # All attempted articles failed to fetch — treat as unreachable rather than ok-with-zero.
        return {
            "site": _site_row("unreachable", sitemap_url, 0, 0, 0.0),
            "articles": [], "images": [], "fields": [],
            "robots_analysis": robots_analysis,
        }

    return {
        "site": _site_row("ok", sitemap_url, len(articles), len(images), mean_score),
        "articles": articles,
        "images": images,
        "fields": fields,
        "robots_analysis": robots_analysis,
    }


def _build_robots_analysis_rows(run_id: str, site_id: str, signals) -> list[RobotsAnalysisRow]:
    """Project the per-UA Protego verdicts into one RobotsAnalysisRow per UA."""
    return [
        RobotsAnalysisRow(
            run_id=run_id, site_id=site_id,
            user_agent=v.ua, operator=v.operator, status=v.status,
        )
        for v in signals.robots_ai.per_ua
    ]


def _family_for(label: str) -> str:
    return "IPTC"  # all current scored fields live in IPTC/XMP; treat as IPTC for the long table


def _sha1_of(s: str) -> str:
    import hashlib
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


__all__ = ["cli"]
