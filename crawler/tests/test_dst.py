from pmd_crawler.dst import (
    extract_dst_from_c2pa_manifest,
    extract_dst_from_xmp,
    short_term,
)


def test_extract_xmp_dst_canonical_tag():
    tags = {"XMP-iptcExt:DigitalSourceType": "http://cv.iptc.org/newscodes/digitalsourcetype/digitalCapture"}
    assert extract_dst_from_xmp(tags) == "http://cv.iptc.org/newscodes/digitalsourcetype/digitalCapture"


def test_extract_xmp_dst_alternate_alias():
    tags = {"XMP:DigitalSourceType": "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"}
    assert extract_dst_from_xmp(tags) == "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"


def test_extract_xmp_dst_list_value():
    # Some exiftool builds emit single-element lists.
    tags = {"XMP-iptcExt:DigitalSourceType": ["http://cv.iptc.org/newscodes/digitalsourcetype/digitalArt"]}
    assert extract_dst_from_xmp(tags) == "http://cv.iptc.org/newscodes/digitalsourcetype/digitalArt"


def test_extract_xmp_dst_absent():
    assert extract_dst_from_xmp({"EXIF:Make": "Canon"}) is None
    assert extract_dst_from_xmp({}) is None


def test_extract_xmp_dst_empty_value():
    assert extract_dst_from_xmp({"XMP-iptcExt:DigitalSourceType": "   "}) is None


def test_extract_c2pa_dst_from_actions_v1():
    manifest = {
        "assertions": [
            {
                "label": "c2pa.actions",
                "data": {
                    "actions": [
                        {"action": "c2pa.created",
                         "digitalSourceType": "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"}
                    ]
                },
            }
        ]
    }
    assert extract_dst_from_c2pa_manifest(manifest) == [
        "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"
    ]


def test_extract_c2pa_dst_from_actions_v2_multiple_dedup():
    manifest = {
        "assertions": [
            {
                "label": "c2pa.actions.v2",
                "data": {
                    "actions": [
                        {"action": "c2pa.created",
                         "digitalSourceType": "http://cv.iptc.org/newscodes/digitalsourcetype/digitalCapture"},
                        {"action": "c2pa.edited",
                         "digitalSourceType": "http://cv.iptc.org/newscodes/digitalsourcetype/humanEdits"},
                        {"action": "c2pa.edited",
                         "digitalSourceType": "http://cv.iptc.org/newscodes/digitalsourcetype/humanEdits"},
                    ]
                },
            }
        ]
    }
    out = extract_dst_from_c2pa_manifest(manifest)
    assert out == [
        "http://cv.iptc.org/newscodes/digitalsourcetype/digitalCapture",
        "http://cv.iptc.org/newscodes/digitalsourcetype/humanEdits",
    ]


def test_extract_c2pa_dst_ignores_unrelated_assertions():
    manifest = {
        "assertions": [
            {"label": "c2pa.thumbnail.claim", "data": {"format": "image/jpeg"}},
            {"label": "stds.exif", "data": {"@context": "..."}},
        ]
    }
    assert extract_dst_from_c2pa_manifest(manifest) == []


def test_extract_c2pa_dst_handles_none_manifest():
    assert extract_dst_from_c2pa_manifest(None) == []
    assert extract_dst_from_c2pa_manifest({}) == []


def test_short_term_canonical_uri():
    assert short_term("http://cv.iptc.org/newscodes/digitalsourcetype/digitalCapture") == "digitalCapture"


def test_short_term_trailing_slash():
    assert short_term("http://cv.iptc.org/newscodes/digitalsourcetype/digitalArt/") == "digitalArt"


def test_short_term_handles_none_and_blanks():
    assert short_term(None) is None
    assert short_term("") is None
