"""IPTC DigitalSourceType extraction.

DST tells us how an image came to exist — captured, scanned, AI-generated,
composited, etc. It's carried in two independent places we already analyse:

* XMP-iptcExt:DigitalSourceType — a single URI value.
* C2PA assertions: c2pa.actions / c2pa.actions.v2 — each action object can
  carry a digitalSourceType parameter.

We store raw URIs end-to-end. Bucket mapping is applied at aggregation time
from config/dst_vocab.yaml so future re-bucketing doesn't require a re-crawl.
"""

from __future__ import annotations

DST_TAG_ALIASES = (
    "XMP-iptcExt:DigitalSourceType",
    "XMP:DigitalSourceType",
    "DigitalSourceType",
    "IPTCExt:DigitalSourceType",
)


def extract_dst_from_xmp(tags: dict[str, object]) -> str | None:
    """Return the DigitalSourceType URI from an exiftool tag dict, if present."""
    lower = {k.lower(): v for k, v in tags.items()}
    for alias in DST_TAG_ALIASES:
        v = lower.get(alias.lower())
        if isinstance(v, str) and v.strip():
            return v.strip()
        # exiftool sometimes emits the value as a one-element list
        if isinstance(v, list) and v and isinstance(v[0], str) and v[0].strip():
            return v[0].strip()
    return None


def extract_dst_from_c2pa_manifest(manifest: dict | None) -> list[str]:
    """Walk a C2PA active manifest dict for digitalSourceType values.

    Looks at all assertions whose label starts with 'c2pa.actions' (covers
    both v1 and v2), and pulls digitalSourceType from each action. Returns
    URIs in the order they appear, deduplicated.
    """
    if not isinstance(manifest, dict):
        return []
    found: list[str] = []
    seen: set[str] = set()
    for assertion in manifest.get("assertions") or []:
        label = assertion.get("label") if isinstance(assertion, dict) else None
        if not isinstance(label, str) or not label.startswith("c2pa.actions"):
            continue
        data = assertion.get("data") if isinstance(assertion, dict) else None
        if not isinstance(data, dict):
            continue
        for action in data.get("actions") or []:
            if not isinstance(action, dict):
                continue
            dst = action.get("digitalSourceType")
            if isinstance(dst, str) and dst.strip() and dst not in seen:
                seen.add(dst)
                found.append(dst.strip())
    return found


def short_term(uri: str | None) -> str | None:
    """Return the last path component of an IPTC newscode URI ('digitalCapture'
    for 'http://cv.iptc.org/newscodes/digitalsourcetype/digitalCapture'). Returns
    None when given None / an empty / non-string value."""
    if not isinstance(uri, str) or not uri:
        return None
    # rsplit handles both http and https, with or without a trailing slash.
    return uri.rstrip("/").rsplit("/", 1)[-1] or None
