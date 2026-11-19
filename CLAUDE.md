# Notes for Claude

Context for Claude Code sessions working in this repo. See
[README.md](README.md) for the human-facing overview and
[SPEC.md](SPEC.md) for the full specification.

## What this repo does

Periodically crawls major news publishers worldwide and reports how often
they preserve embedded photo metadata (EXIF, IPTC IIM, IPTC XMP, C2PA).
The crawler lives in `crawler/`, the per-country publisher lists in
`config/publishers/{cc}.yaml`, monthly run results in `data/runs/`, and
the static site in `site/`. A monthly GitHub Actions workflow runs the
crawl and commits results; the site rebuilds on push to `main`.

## Adding publishers to the database

Publishers live in `config/publishers/{cc}.yaml`, one file per ISO 3166-1
alpha-2 country code (lowercase filename, uppercase `country:` field).
Schema is in `config/publishers/_schema.yaml` and enforced by CI.

**Before setting any `discovery:` URLs, probe the site.** Do not guess
feed/sitemap URLs from CMS patterns — the repo has dedicated tooling for
this and guessed URLs waste crawler budget and pollute health reports.

Workflow for adding a site:

1. Pick a stable kebab-case `id` unique within the country file. When the
   name clashes across countries (e.g. `la-nacion`), suffix with the
   country code (`la-nacion-cr`).
2. Add the entry with `strategy: homepage` and empty `rss_urls` /
   `sitemap_urls` as a placeholder.
3. Run the probe (see below) to find real discovery URLs.
4. Update the entry with what `suggest_feeds.py` found. Use
   `strategy: homepage` only as a last resort when neither RSS nor
   sitemap works.
5. Tag with `suggested_by:` so provenance is visible in diffs (e.g.
   `suggested_by: claude`, `suggested_by: BQ`).

## Scripts

All run from the repo root after `pip install -e './crawler[dev]'`.

| Script | Purpose |
|---|---|
| `pmd-crawler smoke-test --site-id X` | Quick end-to-end check of one site's discovery + article fetch + image metadata. |
| `pmd-crawler run --site-id ...` | Full crawl; writes Parquet to `--output`. |
| `crawler/scripts/probe_publishers.py --country XX` | Checks every publisher's homepage/RSS/sitemap URLs resolve and parse. Does NOT fetch articles. Writes JSON to `--out`. |
| `crawler/scripts/suggest_feeds.py --probe <file>` | For every `rss_dead` site in a probe JSON: scans homepage `<link rel=alternate>` and common sitemap paths (incl. `robots.txt`) for replacements. |
| `crawler/scripts/export_for_site.py` | Exports the latest run to JSON for the Astro site. |
| `crawler/scripts/import_v1_list.py` | One-off: imports the legacy publisher list. |

**The right pattern for a batch of new publishers is:**

```bash
# 1. Add stubs to config/publishers/{cc}.yaml with strategy: homepage
# 2. Probe (per country, or omit --country for all)
.venv/bin/python crawler/scripts/probe_publishers.py --country XX \
  --out data/probes/$(date -u +%Y-%m-%d)-XX.json

# 3. Suggest feeds for anything dead
.venv/bin/python crawler/scripts/suggest_feeds.py \
  --probe data/probes/$(date -u +%Y-%m-%d)-XX.json

# 4. Update YAML with what came back; re-probe to confirm green
```

## Cloud session caveat

**Claude Code cloud sessions run in a sandboxed network environment that
cannot reach arbitrary news hosts** — the Default environment's HTTPS
proxy allowlists only package registries and MCP servers, and every
publisher domain returns 403 at CONNECT. This means:

- `probe_publishers.py`, `suggest_feeds.py`, and `pmd-crawler` cannot run
  in a cloud session against real sites.
- Do config edits (adding publisher entries, changing scoring, editing
  site code) in a cloud session.
- Run probes and crawls locally, or via `.github/workflows/`. The
  monthly crawl workflow already runs on GitHub Actions where outbound
  is open.

If a batch of publisher additions is drafted in a cloud session, leave a
note on the PR that discovery blocks need a local probe pass before
merge. Do not commit probe-result JSON from a cloud session — it's all
403s and misleading.

## Review workflow

- One topic per branch, PR for review, no direct push to `main`.
- CI validates `config/publishers/*.yaml` against the schema.
- The `data/runs/` tree is append-only — crawl runs are committed by the
  scheduled workflow, not by hand.

## Things to avoid

- Guessing RSS/sitemap URLs from CMS patterns (`/feed/`, `/rss`, etc.)
  without probing. Verify with `probe_publishers.py` first.
- Committing probe JSON produced in a cloud session (all 403s).
- Renaming or re-IDing an existing publisher without checking
  `data/runs/` — historic runs reference the old `id`.
- Adding wildcard or unrestricted-network entries to the cloud
  environment allowlist just to make a probe work from a cloud session.
  Probe locally or in CI instead.
