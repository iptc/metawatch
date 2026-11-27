"""AI disclosure gathered from every manifest in a C2PA provenance tree.

Mirrors Origin Verify's ai-disclosure.utils: the declaration that matters is
often in an ingredient's manifest, not the active one (Photoshop 28
Generative Expand / Generate Fill). The real-file case is covered in
test_c2pa_remote_manifest.py; these use small synthetic stores.
"""
from __future__ import annotations

import pytest

from pmd_crawler.dst import (
    ai_level_for_dst,
    collect_c2pa_ai_disclosure,
    extract_dst_from_c2pa_manifest,
)

NS = "http://cv.iptc.org/newscodes/digitalsourcetype/"
GEN = NS + "trainedAlgorithmicMedia"
COMPOSITE = NS + "compositeWithTrainedAlgorithmicMedia"
ENHANCED = NS + "algorithmicallyEnhanced"
CAPTURE = NS + "digitalCapture"


def _manifest(actions=(), ingredients=(), label="c2pa.actions.v2", top_dst=None):
    data = {"actions": list(actions)}
    if top_dst:
        data["digitalSourceType"] = top_dst
    return {"assertions": [{"label": label, "data": data}], "ingredients": list(ingredients)}


def _store(active, manifests):
    return {"active_manifest": active, "manifests": manifests}


def _ing(label, rel):
    return {"relationship": rel, "active_manifest": label}


@pytest.mark.parametrize("uri, via_component, level", [
    (GEN, False, "generated"),
    (GEN, True, "contains"),
    (COMPOSITE, False, "contains"),
    (NS + "compositeSynthetic", False, "contains"),
    (ENHANCED, False, "algorithmic"),
    (NS + "algorithmicMedia", False, "algorithmic"),
    (CAPTURE, False, "none-declared"),
    ("https://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia/", False, "generated"),
])
def test_level_for_dst(uri, via_component, level):
    assert ai_level_for_dst(uri, via_component) == level


def test_photoshop_shape_declaration_in_component_ingredient():
    store = _store("ps", {
        "ps": _manifest(
            [{"action": "c2pa.opened"}, {"action": "c2pa.placed"}],
            [_ing(None, "parentOf"), _ing("ff", "componentOf")],
        ),
        "ff": _manifest([{"action": "c2pa.edited", "digitalSourceType": COMPOSITE}]),
    })
    assert extract_dst_from_c2pa_manifest(store["manifests"]["ps"]) == []
    assert collect_c2pa_ai_disclosure(store) == ("contains", [COMPOSITE])


def test_whole_asset_generated_in_component_only_means_contains():
    store = _store("a", {
        "a": _manifest([{"action": "c2pa.placed"}], [_ing("b", "componentOf")]),
        "b": _manifest([{"action": "c2pa.created", "digitalSourceType": GEN}]),
    })
    assert collect_c2pa_ai_disclosure(store) == ("contains", [GEN])


def test_generated_parent_stays_generated():
    store = _store("a", {
        "a": _manifest([{"action": "c2pa.opened"}], [_ing("b", "parentOf")]),
        "b": _manifest([{"action": "c2pa.created", "digitalSourceType": GEN}]),
    })
    assert collect_c2pa_ai_disclosure(store)[0] == "generated"


def test_component_flag_is_inherited_down_the_tree():
    store = _store("a", {
        "a": _manifest([], [_ing("b", "componentOf")]),
        "b": _manifest([], [_ing("c", "parentOf")]),
        "c": _manifest([{"action": "c2pa.created", "digitalSourceType": GEN}]),
    })
    assert collect_c2pa_ai_disclosure(store)[0] == "contains"


def test_strongest_level_wins_and_active_uris_come_first():
    store = _store("a", {
        "a": _manifest([{"action": "c2pa.color_adjustments", "digitalSourceType": ENHANCED}],
                       [_ing("b", "parentOf")]),
        "b": _manifest([{"action": "c2pa.created", "digitalSourceType": GEN}]),
    })
    assert collect_c2pa_ai_disclosure(store) == ("generated", [ENHANCED, GEN])


def test_dst_in_parameters_on_assertion_and_numbered_instances():
    m = {"assertions": [
        {"label": "c2pa.actions", "data": {"actions": [
            {"action": "c2pa.created", "parameters": {"digitalSourceType": CAPTURE}}]}},
        {"label": "c2pa.actions.v2__1", "data": {"digitalSourceType": ENHANCED, "actions": []}},
        {"label": "c2pa.actions.v2.extra", "data": {"actions": [{"digitalSourceType": GEN}]}},
    ]}
    assert extract_dst_from_c2pa_manifest(m) == [CAPTURE, ENHANCED]


def test_absence_is_none_declared_not_negative():
    store = _store("a", {"a": _manifest([{"action": "c2pa.opened"}], [_ing("missing", "parentOf")])})
    assert collect_c2pa_ai_disclosure(store) == ("none-declared", [])


def test_cycle_guard():
    store = _store("a", {
        "a": _manifest([], [_ing("b", "parentOf")]),
        "b": _manifest([{"digitalSourceType": COMPOSITE}], [_ing("a", "parentOf")]),
    })
    assert collect_c2pa_ai_disclosure(store) == ("contains", [COMPOSITE])


@pytest.mark.parametrize("store", [None, {}, {"active_manifest": "x"}, {"manifests": {}}])
def test_malformed_store(store):
    assert collect_c2pa_ai_disclosure(store) == ("none-declared", [])
