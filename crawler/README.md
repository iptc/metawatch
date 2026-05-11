# pmd-crawler (Metawatch)

The Phase 1 crawler. See [../SPEC.md](../SPEC.md) for full specification.

## Quick start

```bash
# Install (uses uv; falls back to pip)
uv sync                              # or: pip install -e .[dev]

# System dependency: ExifTool
brew install exiftool                # macOS
# apt-get install -y exiftool        # Debian/Ubuntu

# Run a single-site smoke test
pmd-crawler smoke-test --site-id ap

# Run a full crawl (writes to ../data/runs/YYYY-MM-DD/)
pmd-crawler run --output ../data/runs/$(date -u +%Y-%m-%d)

# Run a crawl filtered to one country
pmd-crawler run --country US --output ../data/runs/test
```

## Layout

- `src/pmd_crawler/main.py` — CLI entry (`pmd-crawler`)
- `src/pmd_crawler/config.py` — load `config/publishers/*.yaml`
- `src/pmd_crawler/discovery.py` — robots.txt + sitemap parsing
- `src/pmd_crawler/articles.py` — fetch articles, extract image URLs
- `src/pmd_crawler/images.py` — fetch images, run ExifTool, compute scores
- `src/pmd_crawler/cdn.py` — CDN detection from response headers / hosts
- `src/pmd_crawler/scoring.py` — IPTC score calculation
- `src/pmd_crawler/output.py` — Parquet writers
- `src/pmd_crawler/known_uas.yaml` — list of UAs tracked in Phase 3 robots analysis
- `src/pmd_crawler/cdn_rules.yaml` — CDN detection rules
- `scripts/import_v1_list.py` — one-shot import of v1's `feeds_list.csv` into `config/publishers/*.yaml`
