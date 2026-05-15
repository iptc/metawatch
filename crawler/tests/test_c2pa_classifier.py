"""Tests for the C2PA outcome classifier used by export_for_site.py."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "_export_for_site",
    Path(__file__).resolve().parents[1] / "scripts" / "export_for_site.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
classify = _mod._classify_c2pa


def test_valid_state():
    assert classify("Valid", []) == "valid"


def test_data_hash_mismatch_means_modified():
    # The interesting "CDN re-encoded after signing" bucket.
    assert classify("Invalid", ["assertion.dataHash.mismatch"]) == "modified"


def test_modified_dominates_other_failures():
    # If we see data hash mismatch AND untrusted, the modified story takes
    # priority because it's the more interesting/actionable signal.
    assert classify(
        "Invalid", ["signingCredential.untrusted", "assertion.dataHash.mismatch"]
    ) == "modified"


def test_expired_cert():
    assert classify("Invalid", ["signingCredential.expired"]) == "expired"


def test_only_untrusted_issuer():
    # Sole failure is trust list — that's purely our configuration, not a
    # real authenticity problem.
    assert classify("Invalid", ["signingCredential.untrusted"]) == "untrusted_issuer"


def test_other_invalid_fallback():
    assert classify("Invalid", ["something.else"]) == "other_invalid"
    assert classify("Invalid", []) == "other_invalid"
