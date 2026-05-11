"""Per-image IPTC score (see SPEC.md §9).

A field is scored present if it appears in EITHER IPTC-IIM or XMP.
"""

from __future__ import annotations

FIELD_WEIGHTS: list[tuple[str, list[str], int]] = [
    # (label, exiftool tag aliases (case-insensitive), weight)
    ("Creator", ["By-line", "Creator", "Artist", "XMP:Creator", "IPTC:By-line"], 15),
    ("CopyrightNotice", ["CopyrightNotice", "Rights", "Copyright", "XMP:Rights"], 15),
    ("CaptionAbstract", ["Caption-Abstract", "Description", "ImageDescription", "XMP:Description"], 15),
    ("CreditLine", ["Credit", "XMP:Credit"], 10),
    ("Source", ["Source", "XMP:Source"], 5),
    ("ObjectName", ["ObjectName", "Title", "XMP:Title"], 5),
    ("Keywords", ["Keywords", "Subject", "XMP:Subject"], 5),
    ("DateCreated", ["DateCreated", "CreateDate", "DateTimeOriginal", "XMP:DateCreated"], 10),
    ("LocationCreated", ["City", "Country-PrimaryLocationName", "LocationCreatedCity", "XMP-iptcExt:LocationCreated"], 10),
    ("WebStatement", ["WebStatement", "XMP:WebStatement"], 5),
    ("LicensorURL", ["LicensorURL", "Licensor", "XMP-plus:LicensorURL"], 5),
]

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
    """Return (has_exif, has_iptc_iim, has_xmp)."""
    has_exif = any(k.startswith("EXIF:") for k in exif_tags)
    has_iptc = any(k.startswith("IPTC:") for k in exif_tags)
    has_xmp = any(k.startswith("XMP") for k in exif_tags)
    return has_exif, has_iptc, has_xmp
