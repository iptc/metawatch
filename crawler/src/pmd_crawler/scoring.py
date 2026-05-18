"""Per-image IPTC score (see SPEC.md §9).

The scoring rules live in config/scoring.yaml so they can be tuned without
a code change. ``SCORED_FIELDS`` / ``TRACKED_FIELDS`` / ``ALL_FIELDS`` and
``TOTAL_WEIGHT`` are populated at import time from that file.

The headline score is computed from ``SCORED_FIELDS`` only — the "Four Cs"
of news photo provenance (Creator, Copyright, Caption, Credit). The other
fields are recorded for reporting on the per-field breakdown page but do
not affect the score, because their absence can be legitimate (safety
considerations, workflow specifics) and would unfairly penalise publishers.
"""

from __future__ import annotations

from pathlib import Path

import yaml

SCORING_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "scoring.yaml"


def _load_scoring_config(
    path: Path,
) -> tuple[list[tuple[str, list[str], int]], list[tuple[str, list[str]]]]:
    """Return (scored_fields, tracked_fields) parsed from config/scoring.yaml.

    Raises FileNotFoundError or KeyError if the file is missing or malformed —
    the crawler can't run without scoring rules, so failing loudly is correct.
    """
    data = yaml.safe_load(path.read_text())
    scored = [
        (e["label"], list(e["aliases"]), int(e["weight"]))
        for e in data["scored_fields"]
    ]
    tracked = [(e["label"], list(e["aliases"])) for e in data.get("tracked_fields") or []]
    return scored, tracked


SCORED_FIELDS, TRACKED_FIELDS = _load_scoring_config(SCORING_CONFIG_PATH)

# Union view used wherever the code needs "every field we care about" — the
# parquet/metadata_fields row generation, the substantive-IPTC test, the
# population breakdown. Tracked fields get weight 0 here.
ALL_FIELDS: list[tuple[str, list[str], int]] = [
    *SCORED_FIELDS,
    *[(label, aliases, 0) for label, aliases in TRACKED_FIELDS],
]

TOTAL_WEIGHT = sum(w for _, _, w in SCORED_FIELDS)


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
    """Return (score 0-100, per-field presence list).

    The presence list covers every field in ``ALL_FIELDS`` (scored + tracked)
    so downstream metadata_fields tracking can record presence for everything.
    The score itself counts only ``SCORED_FIELDS``.
    """
    presence: list[tuple[str, bool]] = []
    total = 0
    for label, aliases, weight in ALL_FIELDS:
        has = field_present(exif_tags, aliases) if exif_tags else False
        presence.append((label, has))
        if has and weight:
            total += weight
    score = round(100.0 * total / TOTAL_WEIGHT, 2) if TOTAL_WEIGHT else 0.0
    return score, presence


def families_present(exif_tags: dict[str, object]) -> tuple[bool, bool, bool]:
    """Return (has_exif, has_iptc_iim, has_xmp).

    has_iptc_iim requires at least one of the scored-or-tracked fields to be
    present under the IPTC: group with a non-empty value. Bare structural tags
    like IPTC:ApplicationRecordVersion don't qualify.
    """
    has_exif = any(k.startswith("EXIF:") for k in exif_tags)
    has_xmp = any(k.startswith("XMP") for k in exif_tags)
    has_iptc_iim = _has_substantive_iptc(exif_tags)
    return has_exif, has_iptc_iim, has_xmp


def _has_substantive_iptc(exif_tags: dict[str, object]) -> bool:
    lower = {k.lower(): v for k, v in exif_tags.items() if k.startswith("IPTC:")}
    if not lower:
        return False
    for _label, aliases, _w in ALL_FIELDS:
        for alias in aliases:
            a = alias.lower()
            if a.startswith("xmp"):
                continue
            key = a if a.startswith("iptc:") else f"iptc:{a}"
            if key in lower and _truthy(lower[key]):
                return True
    return False
