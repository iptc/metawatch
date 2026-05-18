"""Per-image IPTC score (see SPEC.md §9).

A field is scored present if it appears in EITHER IPTC-IIM or XMP.

The scoring rules live in config/scoring.yaml so they can be tuned without
a code change. ``FIELD_WEIGHTS`` and ``TOTAL_WEIGHT`` here are populated
at import time from that file.
"""

from __future__ import annotations

from pathlib import Path

import yaml

SCORING_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "scoring.yaml"


def _load_field_weights(path: Path) -> list[tuple[str, list[str], int]]:
    """Read config/scoring.yaml into the (label, aliases, weight) tuple form
    the rest of the module expects.

    Raises FileNotFoundError or KeyError if the file is missing or malformed
    — the crawler can't run without scoring rules, so failing loudly is
    correct.
    """
    data = yaml.safe_load(path.read_text())
    out: list[tuple[str, list[str], int]] = []
    for entry in data["fields"]:
        out.append((entry["label"], list(entry["aliases"]), int(entry["weight"])))
    return out


FIELD_WEIGHTS: list[tuple[str, list[str], int]] = _load_field_weights(SCORING_CONFIG_PATH)
TOTAL_WEIGHT = sum(w for _, _, w in FIELD_WEIGHTS)


def field_present(tags: dict[str, object], aliases: list[str]) -> bool:
    """Return True if any alias appears in the tag dict with a non-empty value."""
    lower = {k.lower(): v for k, v in tags.items()}
    for alias in aliases:
        a = alias.lower()
        # exiftool may emit either "Group:Name" or "Name" — match both
        if a in lower and _truthy(lower[a]):
            return True
        # match by suffix when alias is unqualified
        if ":" not in a:
            for k, v in lower.items():
                if k.endswith(":" + a) and _truthy(v):
                    return True
    return False


def _truthy(v: object) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() != ""
    if isinstance(v, list):
        return len(v) > 0 and any(_truthy(x) for x in v)
    return True


def score_image(exif_tags: dict[str, object]) -> tuple[float, list[tuple[str, bool]]]:
    """Return (score 0-100, per-field presence list)."""
    if not exif_tags:
        return 0.0, [(label, False) for label, _, _ in FIELD_WEIGHTS]
    presence: list[tuple[str, bool]] = []
    total = 0
    for label, aliases, weight in FIELD_WEIGHTS:
        has = field_present(exif_tags, aliases)
        presence.append((label, has))
        if has:
            total += weight
    return round(100.0 * total / TOTAL_WEIGHT, 2), presence


def families_present(exif_tags: dict[str, object]) -> tuple[bool, bool, bool]:
    """Return (has_exif, has_iptc_iim, has_xmp).

    has_iptc_iim requires at least one of the 11 weighted fields to be present
    under the IPTC: group with a non-empty value. Bare structural tags like
    IPTC:ApplicationRecordVersion don't qualify.
    """
    has_exif = any(k.startswith("EXIF:") for k in exif_tags)
    has_xmp = any(k.startswith("XMP") for k in exif_tags)
    has_iptc_iim = _has_substantive_iptc(exif_tags)
    return has_exif, has_iptc_iim, has_xmp


def _has_substantive_iptc(exif_tags: dict[str, object]) -> bool:
    lower = {k.lower(): v for k, v in exif_tags.items() if k.startswith("IPTC:")}
    if not lower:
        return False
    for _label, aliases, _w in FIELD_WEIGHTS:
        for alias in aliases:
            a = alias.lower()
            if a.startswith("xmp"):
                continue
            key = a if a.startswith("iptc:") else f"iptc:{a}"
            if key in lower and _truthy(lower[key]):
                return True
    return False
