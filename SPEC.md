# Photo Metadata Crawler v2 — Specification

> Working project name: **Metawatch**. Alternatives still under consideration: *Metalens*, *Metascope*. Final naming TBC.
> Repo name: `metawatch`.
> Code licence: MIT. Data licence: CC-BY 4.0.

## 1. Purpose & goals

Periodically crawl a curated list of major news publishers worldwide, sample their published images, and analyse them for embedded metadata (, IPTC-IIM, IPTC-XMP, C2PA). Publish the results as league tables ranking sites and countries by metadata quality, with per-site detail and time-series trends.

**Primary audience:** IPTC, journalists, researchers, publishers benchmarking themselves.

**Secondary deliverable:** an open dataset others can analyse.

### Goals

- Cover ~250 publishers across ~50+ countries.
- Crawl monthly. No real-time component.
- Cheap to run (target: free tier).
- Reproducible — every published result traceable to a specific crawl run committed to the repo.
- Polite — strict `robots.txt` compliance, identifiable User-Agent, conservative rate limits.

### Non-goals

- Real-time / continuous crawling.
- Comprehensive coverage of every article on every site (we sample).
- Storing full-resolution images long-term (we sample headers/metadata, then discard).
- Becoming a general web archive.

## 2. Architecture

### High level

```
GitHub Actions (monthly cron)
    │
    ▼
Python crawler (single process)
    │  ├─ robots.txt + sitemap discovery
    │  ├─ article HTML fetch + image URL extraction
    │  ├─ image fetch + ExifTool + c2pa-python
    │  └─ site-level metadata (Phase 3)
    ▼
Parquet output  ──►  committed to repo /data/runs/YYYY-MM-DD/
    │
    ▼
Static site generator (Astro)
    │  reads latest run + historical runs
    ▼
GitHub Pages (custom domain)
```

### Why this shape

- **Monthly cadence + ~15k images/run** is a tiny workload. Anything event-driven (Lambda + SQS + DynamoDB) is overkill.
- **GitHub Actions as scheduler** gives us free compute, version-controlled crawl history, reproducible runs, no infra to manage.
- **Parquet committed to repo** means the data is the artefact — anyone can `git clone` and analyse, and time-series queries come for free.
- **Static site** means near-zero hosting cost, instant page loads, no runtime dependencies on the crawler.

### Why not...

- **AWS Lambda + DynamoDB (v1's approach):** overbuilt for monthly cadence; pays for idle infra; harder to reproduce locally.
- **Cloudflare Workers + R2 + D1:** image processing binaries (ExifTool, c2pa-rs) don't fit Workers' runtime.
- **Self-hosted VM + cron:** cheap but adds a server to babysit.
- **ECS Fargate scheduled task:** good fallback if we outgrow GitHub Actions, but unnecessary at current scale.

### Scaling escape hatches

If the monthly run exceeds Actions' 6h job limit:
1. Shard by country (one job per region, run in parallel).
2. Move to ECS Fargate scheduled task.
3. Move heavy image fetches to a separate downloader stage that fans out.

If repo size becomes a concern:
1. Move Parquet to Cloudflare R2; static site fetches via DuckDB-WASM.
2. Migrate hosting to Cloudflare Pages (zero egress to R2).

## 3. Repository layout

```
metawatch/
├── crawler/                    # Python crawler
│   ├── pyproject.toml
│   ├── src/pmd_crawler/
│   │   ├── __init__.py
│   │   ├── main.py             # CLI entry
│   │   ├── discovery.py        # robots.txt + sitemap parsing
│   │   ├── articles.py         # article HTML fetch + image extraction
│   │   ├── images.py           # image fetch + ExifTool + c2pa
│   │   ├── site_metadata.py    # robots.txt analysis, JSON-LD, etc.
│   │   ├── output.py           # Parquet writers
│   │   └── config.py           # site-list YAML loader
│   └── tests/
├── config/                     # human-edited configuration
│   └── publishers/             # per-country publisher lists ({cc}.yaml)
│       ├── _schema.yaml        # JSON Schema for validation
│       ├── ar.yaml
│       ├── at.yaml
│       ├── ...
│       └── us.yaml
├── data/
│   └── runs/
│       └── 2026-06-01/         # one directory per crawl run
│           ├── runs.parquet
│           ├── sites.parquet
│           ├── articles.parquet
│           ├── images.parquet
│           ├── metadata_fields.parquet
│           └── robots_analysis.parquet
├── site/                       # Astro static site
│   ├── package.json
│   ├── astro.config.mjs
│   ├── src/pages/
│   │   ├── index.astro         # global league table
│   │   ├── countries/[cc].astro
│   │   ├── sites/[id].astro
│   │   └── about.astro
│   └── src/lib/data.ts         # Parquet → JSON loaders at build time
├── .github/workflows/
│   ├── crawl.yml               # monthly crawl + commit results
│   ├── build-site.yml          # build + deploy static site
│   └── ci.yml                  # tests, schema validation, linting
├── SPEC.md                     # this file
└── README.md
```

## 4. Site list

### Format

YAML, one file per country (ISO 3166-1 alpha-2 lowercase). One file per country keeps PRs small and reviewable when sites change.

### Schema

```yaml
# config/publishers/us.yaml
country: US
sites:
  - id: ap                                      # short stable ID, used as primary key
    name: Associated Press
    url: https://apnews.com/
    homepage: https://apnews.com/               # crawl this for site-level metadata
    discovery:
      strategy: sitemap                         # sitemap | rss | homepage
      sitemap_urls:                             # optional override; otherwise derived from robots.txt
        - https://apnews.com/news-sitemap-content.xml
      picture_sitemap_url: null                 # if present, preferred for image discovery
      rss_urls: []                              # fallback only
      js_required: false                        # set true for SPAs (uses Playwright)
    sample:
      max_articles: 20                          # per crawl run
      window_days: 30                           # only articles published within N days
    category: news-agency                       # newspaper | news-agency | broadcaster | online | magazine
    language_primary: en
    notes: ""
    suggested_by: original-2021-list
    active: true
  - id: nytimes
    name: The New York Times
    url: https://www.nytimes.com/
    ...
```

### Migration from v1

[feeds_list.csv](../photo-metadata-crawler/tools/feeds_list.csv) (~256 entries, last updated December 2021) is the seed. A one-shot script (`crawler/scripts/import_v1_list.py`) converts it to the new YAML format. Each entry then needs manual validation:

- URL still resolves and is the same publication.
- `robots.txt` permits crawling under our UA.
- Sitemap exists and is in expected format.
- Country code still correct (some merged outlets, some moved).

Aim: validate ~50 sites/week over 5 weeks before Phase 1 launch.

## 5. Crawl pipeline

### Stage 1: Discovery

For each site:

1. Fetch `https://{domain}/robots.txt` with our UA. Parse with `protego`.
2. If `protego` reports our UA disallowed at root → mark site `skipped: robots_disallow`, record decision, move on.
3. Extract all `Sitemap:` directives.
4. Pick discovery target in order:
   1. Site config's `picture_sitemap_url` if set.
   2. Sitemap URL whose path contains `news` or `picture`.
   3. Sitemap index → recurse one level.
   4. Conventional `/sitemap.xml`, `/sitemap_index.xml`, `/news-sitemap.xml`.
   5. RSS URLs from config.
   6. Homepage scrape (last resort).
5. Parse the sitemap (XML, with Google News + image namespaces).
6. Filter URLs by `news:publication_date` within `sample.window_days`.
7. Sample `sample.max_articles` URLs (most recent first).

### Stage 2: Article fetch

For each sampled article URL:

1. Honour robots.txt `Crawl-delay`; default to 1 req/sec/domain with 0.5s jitter.
2. Fetch HTML with `httpx`. If `js_required: true`, use Playwright (Chromium headless).
3. If the sitemap entry already included `<image:image>` blocks, use those URLs directly.
4. Otherwise extract image URLs from:
   - `<meta property="og:image">` (primary lead image)
   - `<article> img[src]`, `<main> img[src]` (fallback)
   - JSON-LD `NewsArticle.image` (most reliable when present)
5. Skip images smaller than 200×200 (icons, avatars, tracking pixels).
6. Cap at top N=5 images per article.
7. Record any JSON-LD `NewsArticle` / `NewsMediaOrganization` blocks for Phase 3.

### Stage 3: Image analysis

For each image URL:

1. HEAD request first; reject if `content-type` not in image whitelist or if `content-length` > 20MB.
2. GET, stream to a temp file. **Capture full response headers** — needed for CDN attribution.
3. Run **ExifTool** in batch mode → JSON output. Capture all tags structured and grouped by family (, IPTC, XMP).
4. Run **c2pa-python** → record presence of manifest, signing entity, validation status.
5. **CDN detection** (see §5b): combine response-header signals, image URL host, and a cached CNAME lookup on the host. Output: `{cdn_provider, cdn_optimizer_active}` per image.
6. Compute **field presence flags** for each metadata field of interest (see §7).
7. Record image dimensions, MIME type, file size, fetch latency, HTTP status.
8. Discard the image file. Store only the image URL, response headers (small selected subset), and extracted metadata — the original is always re-fetchable from the URL if we need to inspect it manually later.

### Stage 4: Site-level metadata (Phase 3)

For each site, once per run:

1. Re-parse `robots.txt` for the AI-bot block matrix (see §8).
2. Fetch homepage HTML (already cached from discovery).
3. Extract JSON-LD blocks: `NewsMediaOrganization`, `Organization`, `WebSite`. Record `name`, `url`, `logo`, `masthead`, `sameAs`, `ethicsPolicy`, `correctionsPolicy`, `missionCoveragePrioritiesPolicy`, `noBylinesPolicy`, `ownershipFundingInfo`, `verificationFactCheckingPolicy`, `diversityPolicy`, `diversityStaffingReport`, `unnamedSourcesPolicy`, `actionableFeedbackPolicy`, `areaServed`, `award`, `brand`, `companyRegistration`, `founder`, `foundingDate`, `foundingLocation`, `funder`, `funding`, `hasCertification`, `hasCredential`, `knowsAbout`, `knowsLanguage`, `nonprofitStatus`, `owns`, `parentOrganization`, `publishingPrinciples`, `subOrganization`, `slogan`, `alternateName`, `description`, `identifier`, `owner`.
4. Probe `/.well-known/tdmrep.json` (TDM Reservation Protocol). Save it.
5. Probe `/trust.txt`. Save it.
6. Probe `/ai.txt` (Spawning proposal — AI training consent signal at finer granularity than robots.txt). Save it; record presence flag and any per-content-type rules.
7. Probe `/llms.txt` and `/llms-full.txt` ([llmstxt.org](https://llmstxt.org)). Capability signal: which publishers expose LLM-friendly structured content. Record presence flag.
8. Probe `/feed.json` (JSON Feed) — modern alternative to RSS. Record presence flag.
9. **Journalism Trust Initiative (JTI) detection.** Reuse the JSON-LD already extracted in step 3 — pattern-match `usageInfo`, `ethicsPolicy`, `publishingPrinciples` URLs against known JTI patterns (e.g. `*.jti-rsf.org`, `journalismtrust*`), and record which JTI-aligned indicators are present (corrections, ownership transparency, sources policy, methodology, etc.).
10. **Trust Project detection.** Same JSON-LD reuse — match URLs containing `thetrustproject.org` and record which Trust Indicators a site cites.
11. **Content licensing signals on homepage HTML:**
    - `<link rel="license" href="…">` — record URL and recognise Creative Commons variants.
    - Any reference to IPTC RightsML, ODRL policies (`xmlns:odrl`, `application/odrl+xml`, `<script type="application/ld+json">` with `@context: "http://www.w3.org/ns/odrl.jsonld"`).
12. Probe `/security.txt`, `/humans.txt` (low priority, useful as completeness signal).
13. **rel="me" links** on homepage `<head>` and footer — record count and target hosts (Mastodon, X, Bluesky, etc.). Identity-verification signal.
14. **WebSub presence** — `<link rel="hub">` in homepage `<head>` or in any discovered RSS/Atom feed. Push-distribution signal.
15. **OpenGraph completeness** on homepage and a sample of articles — record which `og:*` and `article:*` fields are populated. Especially: `og:image:alt`, `og:image:width`, `article:author`, `article:published_time`. Useful to correlate with embedded image metadata practices.

## 5b. CDN detection

CDN image-optimization features strip metadata by default on most major providers. Without attribution, we can't distinguish "the publisher doesn't embed metadata" from "the publisher's CDN strips it on delivery." Both are publishable findings, but they're different stories.

Detection combines three signals (cheap; data we already have or can fetch once per host):

1. **Response headers** captured during image fetch:
   - Cloudflare: `cf-ray`, `cf-cache-status`, `server: cloudflare`
   - CloudFront: `x-amz-cf-id`, `x-amz-cf-pop`, `via: ... CloudFront`
   - Fastly: `x-served-by`, `x-fastly-request-id`, `x-cache` with Fastly-shaped values
   - Akamai: `x-akamai-transformed` (strong: confirms image transformation), `x-akamai-request-id`, `server: AkamaiGHost`
   - imgix: `x-imgix-id`
   - Cloudinary: `x-cld-*`
   - Generic: `via:`, `cdn-loop:`, `server:` substring matches.
2. **Image URL host pattern**: `*.imgix.net`, `*.cloudinary.com`, `*.cloudfront.net`, `*.akamaized.net`, `*.bunnycdn.com`, `*.fastly.net`.
3. **CNAME lookup** of the image host (cached per (run, host)): resolves to one of the above.

Output two columns on `images.parquet`:

- `cdn_provider` — `cloudflare` | `fastly` | `akamai` | `cloudfront` | `imgix` | `cloudinary` | `bunny` | `other` | `none` | `unknown`.
- `cdn_optimizer_active` — `true` when we see provider-specific transformation evidence (e.g. `x-akamai-transformed`, Cloudinary's `f_auto,q_auto` query params, imgix `auto=` params, Cloudflare `cf-polish` headers). `false` when CDN is detected but only used for caching. `unknown` otherwise.

This lets us produce a dedicated page `/cdn/` showing:

- Which CDNs dominate news image delivery globally.
- Among sites scoring zero on IPTC presence, what % are behind a CDN with active optimization.
- A site's "self-strip vs CDN-strip" inference: if metadata is preserved when we fetch through one CDN path but absent on another, that's a useful signal.

CDN detection lives in `crawler/src/pmd_crawler/cdn.py` with a rules table editable in `crawler/src/pmd_crawler/cdn_rules.yaml`.

## 6. Politeness & identification

- **User-Agent:** `IPTCMetadataCrawler/2.0 (+https://github.com/iptc/metawatch; metadata-crawler@iptc.org)`.
  - Deliberately doesn't pattern-match `bot`, `Claude`, `GPT`, `anthropic`, `AI` — we are not an AI bot and don't want to be lumped in with them.
- Strict per-UA `robots.txt` compliance with `protego`.
- Default 1 req/sec/domain; honour `Crawl-delay` if higher.
- `Accept-Encoding: gzip`, `If-Modified-Since` where possible.
- No JS execution unless site config requires it (smaller footprint).
- Maintain `config/publishers/_optouts.yaml` — any publisher who emails us asking to be removed gets added here without question.

## 7. Output data model (Parquet)

One Parquet file per logical table per run, written to `data/runs/{YYYY-MM-DD}/`.

### `runs.parquet` (1 row per run)

| field | type | notes |
|---|---|---|
| run_id | string | `YYYY-MM-DD-HHMMSS` |
| started_at | timestamp | |
| ended_at | timestamp | |
| crawler_version | string | git sha |
| site_count_attempted | int | |
| site_count_succeeded | int | |
| site_count_robots_blocked | int | |
| article_count | int | |
| image_count | int | |

### `sites.parquet` (1 row per site per run)

| field | type | notes |
|---|---|---|
| run_id | string | |
| site_id | string | from YAML |
| country | string | ISO alpha-2 |
| category | string | |
| status | string | `ok` \| `robots_disallow` \| `unreachable` \| `no_articles_found` |
| robots_url | string | |
| sitemap_url_used | string | |
| discovery_strategy | string | which tier succeeded |
| articles_sampled | int | |
| images_analysed | int | |
| has_news_media_org_jsonld | bool | Phase 3 |
| has_content_signals | bool | Phase 3 — Cloudflare `Content-Signal:` directive present in robots.txt |
| content_signal_ai_train | string | Phase 3 — `yes`/`no`/null |
| content_signal_ai_input | string | Phase 3 — `yes`/`no`/null |
| content_signal_search | string | Phase 3 — `yes`/`no`/null |
| has_tdmrep | bool | Phase 3 |
| has_trust_txt | bool | Phase 3 |
| has_ai_txt | bool | Phase 3 |
| has_llms_txt | bool | Phase 3 |
| has_json_feed | bool | Phase 3 |
| has_jti_indicators | bool | Phase 3 — JTI detected in JSON-LD |
| jti_indicator_count | int | Phase 3 — number of JTI fields populated |
| has_trust_project | bool | Phase 3 — Trust Project URLs referenced |
| has_link_license | bool | Phase 3 — `<link rel="license">` on homepage |
| link_license_url | string | Phase 3 — nullable |
| has_rightsml_or_odrl | bool | Phase 3 |
| has_security_txt | bool | Phase 3 |
| has_humans_txt | bool | Phase 3 |
| rel_me_count | int | Phase 3 |
| has_websub | bool | Phase 3 |
| og_field_coverage | float | Phase 3 — fraction of expected OG fields populated on homepage |

### `articles.parquet` (1 row per article)

| field | type |
|---|---|
| run_id | string |
| site_id | string |
| article_url | string |
| article_url_hash | string (sha1, indexable) |
| publication_date | timestamp |
| title | string |
| language | string |
| keywords | list<string> |
| jsonld_news_article | string (JSON) |
| http_status | int |
| fetched_at | timestamp |
| tdm_reservation | int | Phase 3 — value of `<meta name="tdm-reservation">`; 0/1/null |

### `images.parquet` (1 row per image)

| field | type |
|---|---|
| run_id | string |
| site_id | string |
| article_url_hash | string |
| image_url | string |
| image_url_hash | string |
| mime_type | string |
| width | int |
| height | int |
| file_size_bytes | int |
| http_status | int |
| has_exif | bool |
| has_iptc_iim | bool |
| has_iptc_xmp | bool |
| has_c2pa | bool |
| c2pa_manifest_signer | string (nullable) |
| c2pa_validation_status | string (nullable) |
| cdn_provider | string | `cloudflare` \| `fastly` \| `akamai` \| `cloudfront` \| `imgix` \| `cloudinary` \| `bunny` \| `other` \| `none` \| `unknown` |
| cdn_optimizer_active | string | `true` \| `false` \| `unknown` |
| metadata_field_count | int | total count of populated fields |
| iptc_score | float | weighted compliance score, see §9 |

### `metadata_fields.parquet` (1 row per (image, field) tuple)

Long format. Lets us answer "which specific fields are most/least populated."

| field | type |
|---|---|
| run_id | string |
| image_url_hash | string |
| family | string | `Exif` \| `IPTC-IIM` \| `XMP` \| `C2PA` |
| field_name | string | e.g. `Creator`, `Copyright`, `CaptionAbstract` |
| has_value | bool |

### `robots_analysis.parquet` (Phase 3, 1 row per (site, ua) tuple)

| field | type |
|---|---|
| run_id | string |
| site_id | string |
| user_agent | string | from a fixed list of ~50 well-known UAs |
| status | string | `allowed` \| `disallowed` \| `partial` |
| disallowed_paths | list<string> | when `partial` |

## 8. Tracked AI/scraper UAs (Phase 3)

Fixed list, evaluated against each site's `robots.txt`. Primary source is
Appendix A of the [IPTC Generative AI Opt-Out Best Practice Recommendations
v2.0](https://iptc.org/std/guidelines/data-mining-opt-out/IPTC-Generative-AI-Opt-Out-Best-Practices-v2.0.pdf),
with a handful of additions for operators the IPTC list doesn't yet cover
(DeepSeek, Mistral, xAI, etc.). 60+ UAs as of the current version.

The authoritative list is versioned in `crawler/src/pmd_crawler/known_uas.yaml`
and updated as the landscape evolves.

## 9. Scoring

A per-image **IPTC score** (0–100) based on presence of a weighted set of fields. Initial weights (subject to review):

| field | family | weight |
|---|---|---|
| Creator / By-line | IPTC + XMP | 15 |
| CopyrightNotice | IPTC + XMP | 15 |
| CaptionAbstract / Description | IPTC + XMP | 15 |
| CreditLine | IPTC + XMP | 10 |
| Source | IPTC + XMP | 5 |
| ObjectName / Title | IPTC + XMP | 5 |
| Keywords | IPTC + XMP | 5 |
| DateCreated | IPTC + XMP | 10 |
| LocationCreated (City/Country) | XMP | 10 |
| WebStatement | XMP | 5 |
| LicensorURL | XMP | 5 |

Per-field, score the field as present if it appears in **either** IPTC-IIM **or** XMP (publishers vary in which they use; what matters is that the data is *somewhere*).

**Site score** = mean of image scores. **Country score** = mean of site scores (not images — equal weight per site).

C2PA gets its own separate score for now: % of images carrying a valid manifest. Don't fold into the IPTC score.

## 10. Static site

### Stack

- **Astro** (static output mode).
- **Observable Plot** for charts (small, dependency-light, good defaults).
- **react-simple-maps** kept from v1 for the world map (works fine in Astro via island).
- Site is fully static — no runtime data fetching. All Parquet → JSON conversion happens at build time via DuckDB CLI in the build step.

### Pages

| route | content |
|---|---|
| `/` | Global rankings: top 10 sites by IPTC score, top 10 countries; world map; latest run summary; trend chart over runs. |
| `/countries/` | All countries ranked. |
| `/countries/{cc}/` | One country: list of sites + their scores; comparison chart. |
| `/sites/` | All sites ranked; filterable. |
| `/sites/{id}/` | One site: score over time; per-field breakdown; sample article + image grid (just URLs and metadata snapshots, no images stored); recent crawl details (status, robots verdict, sitemap used). |
| `/fields/` | Which IPTC fields are most/least populated globally. |
| `/c2pa/` | C2PA adoption (Phase 1: probably reads "0 manifests found across N images"). |
| `/cdn/` | CDN distribution; self-strip vs CDN-strip analysis. |
| `/ai-policy/` | AI bot block matrix + `ai.txt` adoption (Phase 3). |
| `/trust/` | Journalism Trust Initiative + Trust Project adoption; cross-axis with IPTC score (Phase 3). |
| `/licensing/` | `rel="license"`, RightsML/ODRL, TDMRep — content licensing signals across the dataset (Phase 3). |
| `/standards/` | Adoption of less-common standards we record (`llms.txt`, JSON Feed, WebSub, `rel="me"`, security.txt) — completeness scoreboard (Phase 3). |
| `/about/` | Methodology, FAQ, dataset download links, contact (publishers wishing to opt out can email office@iptc.org). |

### Data dataset page

Link to:
- Latest run's `images.parquet` and `metadata_fields.parquet` (raw download).
- A DuckDB query example block with copy-pasteable SQL.
- Schema documentation generated from §7.

## 11. GitHub Actions

### `crawl.yml`

```yaml
name: Monthly crawl
on:
  schedule:
    - cron: "0 2 1 * *"          # 02:00 UTC on the 1st of each month
  workflow_dispatch:             # allow manual runs

jobs:
  crawl:
    runs-on: ubuntu-latest
    timeout-minutes: 350         # < 6h job limit
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e ./crawler
      - run: sudo apt-get install -y exiftool
      - run: pmd-crawler run --output data/runs/$(date -u +%Y-%m-%d)
      - name: Commit results
        run: |
          git config user.name "pmd-crawler-bot"
          git config user.email "metadata-crawler@iptc.org"
          git add data/runs/
          git commit -m "Crawl run $(date -u +%Y-%m-%d)" || exit 0
          git push
```

### `build-site.yml`

Triggered on push to `main` under `data/runs/**` or `site/**`. Builds Astro site, deploys to GitHub Pages.

### `ci.yml`

On PR: validate `config/publishers/*.yaml` against the JSON schema, run crawler unit tests, lint Python, typecheck TypeScript.

## 12. Phasing

### Phase 1 (MVP, ~6 weeks)

- Crawler covering: robots.txt → sitemap → article HTML → image fetch → ExifTool.
- **CDN detection** included from the start — it's cheap and load-bearing for interpreting the headline result.
- Site list YAML migrated from v1 CSV; ~250 sites validated. Wire services kept in their home country (AP→US, AFP→FR, Reuters→GB).
- Parquet output for `runs`, `sites`, `articles`, `images`, `metadata_fields`.
- Static site with `/`, `/countries/`, `/sites/`, `/sites/{id}/`, `/cdn/`, `/about/`.
- Monthly cron, results committed to repo.
- **Skipped for this phase:** C2PA, robots analysis matrix, JSON-LD organisation extraction, tdmrep/trust.txt.

### Phase 2 (~3 weeks after Phase 1)

- C2PA support via `c2pa-python`.
- `/c2pa/` page.
- Per-field breakdown page `/fields/`.
- Time-series charts (need ≥2 runs).
- Public dataset page with download links.

### Phase 3 (~4 weeks after Phase 2)

- AI bot block matrix (`robots_analysis.parquet`, `/ai-policy/`).
- `NewsMediaOrganization` JSON-LD extraction from homepage.
- `tdmrep.json` and `trust.txt` probes.
- **`ai.txt` and `llms.txt` probes.**
- **Journalism Trust Initiative (JTI) detection** from JSON-LD; per-indicator presence flags.
- **Trust Project detection** from JSON-LD.
- **Content licensing signals:** `<link rel="license">`, RightsML/ODRL references.
- **Identity/distribution signals:** `rel="me"` links, WebSub, JSON Feed.
- **OpenGraph completeness** baseline.
- `security.txt`, `humans.txt` probes.
- New pages: `/trust/`, `/licensing/`, `/standards/`.
- "Cross-axis" pages (e.g. "of sites declaring a verification policy, how many strip image metadata?", "do JTI-aligned publishers retain more Creator metadata than non-JTI?").

## 13. Decisions made

- **Working name:** Metawatch (final TBC). Shortlisted alternatives: Metalens, Metascope.
- **Repo name:** `metawatch`.
- **Code licence:** MIT. Repo public.
- **Data licence:** CC-BY 4.0 on published Parquet.
- **Opt-out:** publishers email office@iptc.org. Documented on `/about/`.
- **News agencies:** assigned to their home country (AP→US, AFP→FR, Reuters→GB). No separate ranking — they sit in the country tables alongside others. `category: news-agency` remains as a filter for cross-cuts ("agencies vs publishers").
- **Image storage:** none. Only the image URL is stored; if a result needs verification we re-fetch.
- **Historical data:** no import from v1.

## 14. Still open

1. **Hosting domain.** Live at <https://metawatch.iptc.org/> (GitHub Pages + DNS CNAME).

## 15. Future considerations

### Member features

Possible member-only features are discussed separately from this spec. For the current build: keep the data model and the static-site architecture clean enough that an authenticated layer could be added later, without baking a public/private split into the schema.

## 16. Out of scope (for now)

- Video metadata (some sites have `<video>` tags with -equivalent metadata).
- PDF metadata (publishers occasionally embed images in PDFs).
- Social-media-platform analysis (Facebook, X, Instagram strip metadata aggressively — known result).
- Advertising images (we focus on editorial content).
- Per-photographer analysis (interesting but requires entity resolution we don't want to maintain).
