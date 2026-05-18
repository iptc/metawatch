from pmd_crawler.scoring import (
    ALL_FIELDS,
    SCORED_FIELDS,
    TOTAL_WEIGHT,
    TRACKED_FIELDS,
    families_present,
    score_image,
)


def test_four_cs_are_the_scored_fields():
    # Lock the methodology: only the Four Cs of news photo provenance
    # contribute to the score. Tracked fields are recorded but worth 0.
    assert {label for label, _, _ in SCORED_FIELDS} == {
        "Creator", "Copyright", "CaptionDescription", "CreditLine",
    }
    # Equal weights summing to 100.
    assert TOTAL_WEIGHT == 100
    assert all(w == 25 for _, _, w in SCORED_FIELDS)


def test_tracked_fields_are_recorded_but_unscored():
    # Tracked = present in ALL_FIELDS with weight 0; not in SCORED_FIELDS.
    scored_labels = {label for label, _, _ in SCORED_FIELDS}
    tracked_labels = {label for label, _ in TRACKED_FIELDS}
    assert tracked_labels & scored_labels == set()
    # All tracked fields appear in ALL_FIELDS as weight-0 entries.
    weight_by_label = {label: weight for label, _, weight in ALL_FIELDS}
    for label in tracked_labels:
        assert weight_by_label[label] == 0


def test_empty_tags_score_zero_and_no_families():
    score, presence = score_image({})
    assert score == 0.0
    assert all(not p for _, p in presence)
    assert families_present({}) == (False, False, False)


def test_iptc_application_record_version_alone_is_not_substantive():
    # Regression: previously has_iptc_iim was True for any IPTC: tag,
    # including the bare ApplicationRecordVersion marker.
    tags = {"IPTC:ApplicationRecordVersion": 4}
    has_exif, has_iptc, has_xmp = families_present(tags)
    assert has_iptc is False
    assert score_image(tags)[0] == 0.0


def test_weighted_iptc_field_is_substantive():
    tags = {"IPTC:By-line": "Jane Doe"}
    _, has_iptc, _ = families_present(tags)
    assert has_iptc is True


def test_xmp_only_does_not_flag_iptc_iim():
    tags = {"XMP:Creator": "Jane Doe"}
    has_exif, has_iptc, has_xmp = families_present(tags)
    assert (has_iptc, has_xmp) == (False, True)


def test_full_score_from_the_four_cs_only():
    # With only the Four Cs present we should still hit 100 — that's the
    # whole point of the new weighting.
    tags = {
        "IPTC:By-line": "Jane Doe",
        "IPTC:CopyrightNotice": "(c) 2026",
        "IPTC:Caption-Abstract": "A photo.",
        "IPTC:Credit": "Wire Service",
    }
    assert score_image(tags)[0] == 100.0


def test_tracked_fields_dont_lift_the_score():
    # Source, ObjectName, Keywords, DateCreated, LocationCreated,
    # WebStatement, LicensorURL are tracked but unscored. An image with only
    # those should still score 0.
    tags = {
        "IPTC:Source": "Agency",
        "IPTC:ObjectName": "Title",
        "IPTC:Keywords": ["news"],
        "IPTC:DateCreated": "2026:05:12",
        "IPTC:City": "London",
        "XMP:WebStatement": "https://example.com/terms",
        "XMP-plus:LicensorURL": "https://example.com/licensor",
    }
    score, presence = score_image(tags)
    assert score == 0.0
    # …but presence is still recorded for every tracked field so we don't
    # lose data for the /fields/ page.
    present_labels = {label for label, has in presence if has}
    assert "Source" in present_labels
    assert "Keywords" in present_labels
    assert "LocationCreated" in present_labels


def test_partial_score_from_some_cs():
    # Two of four Cs present → 50.
    tags = {
        "IPTC:By-line": "Jane Doe",
        "IPTC:Caption-Abstract": "A photo.",
    }
    assert score_image(tags)[0] == 50.0


def test_empty_string_value_is_not_present():
    tags = {"IPTC:By-line": "   "}
    assert families_present(tags)[1] is False


def test_xmp_dst_alone_counts_as_xmp_present():
    # DigitalSourceType lives in XMP-iptcExt and should make has_iptc_xmp True
    # on its own, so the "% with any IPTC" rollup catches DST-only images.
    tags = {"XMP-iptcExt:DigitalSourceType": "http://cv.iptc.org/newscodes/digitalsourcetype/digitalCapture"}
    has_exif, has_iim, has_xmp = families_present(tags)
    assert (has_exif, has_iim, has_xmp) == (False, False, True)
