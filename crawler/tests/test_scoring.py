from pmd_crawler.scoring import TOTAL_WEIGHT, families_present, score_image


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


def test_full_score_when_all_fields_present_via_iptc():
    tags = {
        "IPTC:By-line": "Jane Doe",
        "IPTC:CopyrightNotice": "(c) 2026",
        "IPTC:Caption-Abstract": "A photo.",
        "IPTC:Credit": "Wire Service",
        "IPTC:Source": "Agency",
        "IPTC:ObjectName": "Title",
        "IPTC:Keywords": ["news"],
        "IPTC:DateCreated": "2026:05:12",
        "IPTC:City": "London",
        "XMP:WebStatement": "https://example.com/terms",
        "XMP-plus:LicensorURL": "https://example.com/licensor",
    }
    score, _ = score_image(tags)
    assert score == round(100.0 * TOTAL_WEIGHT / TOTAL_WEIGHT, 2) == 100.0


def test_empty_string_value_is_not_present():
    tags = {"IPTC:By-line": "   "}
    assert families_present(tags)[1] is False


def test_xmp_dst_alone_counts_as_xmp_present():
    # DigitalSourceType lives in XMP-iptcExt and should make has_iptc_xmp True
    # on its own, so the "% with any IPTC" rollup catches DST-only images.
    tags = {"XMP-iptcExt:DigitalSourceType": "http://cv.iptc.org/newscodes/digitalsourcetype/digitalCapture"}
    has_exif, has_iim, has_xmp = families_present(tags)
    assert (has_exif, has_iim, has_xmp) == (False, False, True)
