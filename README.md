# Metawatch

Periodically scans major news publishers worldwide and reports how often they preserve embedded photo metadata (EXIF, IPTC IIM, IPTC XMP, C2PA). A project of [IPTC](https://iptc.org/).

See [SPEC.md](SPEC.md) for the full specification.

## Repository layout

```
crawler/             Python crawler (CLI: pmd-crawler)
config/publishers/   Per-country YAML publisher lists ({cc}.yaml)
data/runs/           Crawl results, one directory per monthly run (Parquet)
site/                Astro static site (deployed to GitHub Pages)
.github/             CI + scheduled crawl workflows
```

## Quick start

```bash
# One-time setup
python3 -m venv .venv
.venv/bin/pip install -e './crawler[dev]'
.venv/bin/pre-commit install      # ruff + whitespace checks on every commit

brew install exiftool             # macOS — or apt-get install exiftool

# Smoke-test discovery against one site
.venv/bin/pmd-crawler smoke-test --site-id ap

# Run a small crawl (one or a few sites)
.venv/bin/pmd-crawler run \
  --site-id ap,reuters,bbc-news,ny-times,le-monde \
  --output data/runs/$(date -u +%Y-%m-%d)-smoke

# Export the latest run to JSON for the site
.venv/bin/python crawler/scripts/export_for_site.py

# Dev-preview the static site
cd site && npm install && npm run dev
```

## Production

- The monthly crawl is triggered by `.github/workflows/crawl.yml` (cron, 02:00 UTC on the 1st) and commits the run to `data/runs/`.
- The static site is rebuilt and deployed to GitHub Pages by `.github/workflows/build-site.yml` whenever `site/` or `data/runs/` changes on `main`.
- Deployed at <https://metawatch.iptc.org/>.

## Licences

- Code: MIT (see [LICENSE](LICENSE)).
- Data: CC BY 4.0.
