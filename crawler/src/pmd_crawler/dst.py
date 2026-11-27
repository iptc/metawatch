"""IPTC DigitalSourceType extraction.

DST tells us how an image came to exist — captured, scanned, AI-generated,
composited, etc. It's carried in two independent places we already analyse:

* XMP-iptcExt:DigitalSourceType — a single URI value.
* C2PA assertions: c2pa.actions / c2pa.actions.v2 — each action object can
  carry a digitalSourceType parameter. Declarations may sit in any manifest
  of the provenance tree, not just the active one (see
  collect_c2pa_ai_disclosure).

We store raw URIs end-to-end. Bucket mapping is applied at aggregation time
from config/dst_vocab.yaml so future re-bucketing doesn't require a re-crawl.
"""

from __future__ import annotations

import re

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


_ACTIONS_LABEL = re.compile(r"^c2pa\.actions(\.v2)?(__\d+)?$")


def _actions_dsts(manifest: dict | None) -> list[dict]:
    """Every digitalSourceType a manifest's actions assertions carry.

    Looks on each action, in an action's ``parameters`` (older writers), and
    on the assertion itself; covers ``c2pa.actions``, ``c2pa.actions.v2`` and
    further instances (``c2pa.actions.v2__1``, ...). Returns dicts with
    ``uri``, ``action`` and ``software_agent`` in document order.
    """
    if not isinstance(manifest, dict):
        return []
    out: list[dict] = []
    for assertion in manifest.get("assertions") or []:
        if not isinstance(assertion, dict):
            continue
        label = assertion.get("label")
        if not isinstance(label, str) or not _ACTIONS_LABEL.match(label):
            continue
        data = assertion.get("data")
        if not isinstance(data, dict):
            continue
        top = data.get("digitalSourceType")
        if isinstance(top, str) and top.strip():
            out.append({"uri": top.strip(), "action": None, "software_agent": None})
        for action in data.get("actions") or []:
            if not isinstance(action, dict):
                continue
            params = action.get("parameters") if isinstance(action.get("parameters"), dict) else {}
            dst = action.get("digitalSourceType")
            if not isinstance(dst, str):
                dst = params.get("digitalSourceType")
            if isinstance(dst, str) and dst.strip():
                agent = action.get("softwareAgent")
                if isinstance(agent, dict):
                    agent = agent.get("name")
                out.append({
                    "uri": dst.strip(),
                    "action": action.get("action") if isinstance(action.get("action"), str) else None,
                    "software_agent": agent.strip() if isinstance(agent, str) and agent.strip() else None,
                })
    return out


def _dedupe(uris) -> list[str]:
    seen: set[str] = set()
    return [u for u in uris if not (u in seen or seen.add(u))]


def extract_dst_from_c2pa_manifest(manifest: dict | None) -> list[str]:
    """digitalSourceType URIs declared by one manifest (normally the active
    one), in document order, deduplicated. See ``_actions_dsts`` for where
    they are looked for. For declarations further down the provenance tree
    use ``collect_c2pa_ai_disclosure``."""
    return _dedupe(d["uri"] for d in _actions_dsts(manifest))


# ─── AI disclosure over the whole provenance tree ───────────────────────────
#
# Mirrors Origin Verify's ai-disclosure.utils (origin-verify-validator,
# 2026-11-27). The declaration that matters is often not in the active
# manifest: Photoshop 28's Generative Expand signs a top-level manifest whose
# only actions are c2pa.opened / c2pa.placed with no digitalSourceType, and
# the AI declaration (compositeWithTrainedAlgorithmicMedia) sits in the
# Firefly manifest of the componentOf ingredient it placed.
#
# Absence proves nothing — a tool that used AI may not say so — so the lowest
# level is "none-declared", never "not AI".

AI_LEVELS = ("generated", "contains", "algorithmic", "none-declared")  # strongest first
_LEVEL_RANK = {"generated": 3, "contains": 2, "algorithmic": 1, "none-declared": 0}
_GENERATIVE_WHOLE = {"trainedAlgorithmicMedia"}
_GENERATIVE_COMPOSITE = {"compositeWithTrainedAlgorithmicMedia", "compositeSynthetic"}
_ALGORITHMIC = {"algorithmicallyEnhanced", "algorithmicMedia"}


def ai_level_for_dst(uri: str, via_component: bool = False) -> str:
    """Level a single digitalSourceType implies. A whole-asset generative term
    reached through a componentOf ingredient only means the delivered asset
    *contains* generated material."""
    term = short_term(uri.strip()) or ""
    if term in _GENERATIVE_WHOLE:
        return "contains" if via_component else "generated"
    if term in _GENERATIVE_COMPOSITE:
        return "contains"
    if term in _ALGORITHMIC:
        return "algorithmic"
    return "none-declared"


def stronger_ai_level(a: str, b: str) -> str:
    return b if _LEVEL_RANK.get(b, 0) > _LEVEL_RANK.get(a, 0) else a


def _ingredient_manifest_label(ingredient: dict) -> str | None:
    for key in ("active_manifest", "activeManifest"):
        v = ingredient.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def collect_c2pa_ai_disclosure(store: dict | None) -> tuple[str, list[str]]:
    """Walk a c2pa-rs manifest store (``Reader.json()``) from the active
    manifest through ingredients that carry their own manifest.

    Returns ``(level, uris)``: the strongest AI level declared anywhere in the
    tree, and every distinct digitalSourceType URI found (active manifest
    first, then depth-first). Ingredients without a manifest in the store
    contribute nothing — a gap in the evidence, not a negative. A cycle guard
    drops revisited manifests.
    """
    if not isinstance(store, dict):
        return "none-declared", []
    manifests = store.get("manifests")
    active = store.get("active_manifest")
    if not isinstance(manifests, dict) or not isinstance(active, str):
        return "none-declared", []

    level = "none-declared"
    uris: list[str] = []
    visited: set[str] = set()

    def walk(label: str, via_component: bool) -> None:
        nonlocal level
        if label in visited or not isinstance(manifests.get(label), dict):
            return
        visited.add(label)
        manifest = manifests[label]
        for d in _actions_dsts(manifest):
            uris.append(d["uri"])
            level = stronger_ai_level(level, ai_level_for_dst(d["uri"], via_component))
        for ing in manifest.get("ingredients") or []:
            if not isinstance(ing, dict):
                continue
            child = _ingredient_manifest_label(ing)
            if child:
                rel = ing.get("relationship")
                is_component = isinstance(rel, str) and rel.lower() == "componentof"
                walk(child, via_component or is_component)

    walk(active, False)
    return level, _dedupe(uris)


def short_term(uri: str | None) -> str | None:
    """Return the last path component of an IPTC newscode URI ('digitalCapture'
    for 'http://cv.iptc.org/newscodes/digitalsourcetype/digitalCapture'). Returns
    None when given None / an empty / non-string value."""
    if not isinstance(uri, str) or not uri:
        return None
    # rsplit handles both http and https, with or without a trailing slash.
    return uri.rstrip("/").rsplit("/", 1)[-1] or None
