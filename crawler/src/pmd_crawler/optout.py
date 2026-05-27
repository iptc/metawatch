"""AI opt-out signal probes.

Phase 3 work. Collects machine-readable opt-out signals across the eight
mechanisms tracked on the /ai-policy/ page:

  Site-wide  -- robots.txt AI-bot matrix, /.well-known/tdmrep.json, /ai.txt,
                robots.txt RSL `License:` directive, /.well-known/trust.txt
                (datatrainingallowed=).
  Image-level - noai/noimageai in HTTP response headers or article <meta robots>,
                CAWG training-and-data-mining assertion in C2PA manifests, and
                the IPTC PLUS:DataMining XMP field (already extracted upstream).

This module owns the site-wide signals only. Image-level signals are detected
inline in articles.py / images.py and surfaced on the ImageRow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

import httpx
import yaml
from protego import Protego

# ──────────────────────────────────────────────────────────────────────────────
# Known UA registry — loaded once at import time. Tiny file, no hot reload.
# ──────────────────────────────────────────────────────────────────────────────

_KNOWN_UAS_PATH = Path(__file__).parent / "known_uas.yaml"


@dataclass(frozen=True)
class TrackedUA:
    ua: str
    operator: str


def _load_known_uas() -> list[TrackedUA]:
    data = yaml.safe_load(_KNOWN_UAS_PATH.read_text())
    return [TrackedUA(ua=e["ua"], operator=e["operator"]) for e in data["uas"]]


KNOWN_UAS: list[TrackedUA] = _load_known_uas()


# ──────────────────────────────────────────────────────────────────────────────
# robots.txt analysis
# ──────────────────────────────────────────────────────────────────────────────

# Match `License: <url>` lines (RSL), case-insensitive on the directive name.
# RSL spec: https://rslstandard.org/guide/robots-txt
_RSL_LINE_RE = re.compile(r"^\s*License\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)


@dataclass
class AiBotVerdict:
    ua: str
    operator: str
    status: str  # "allowed" | "disallowed"


@dataclass
class RobotsAiAnalysis:
    """Result of running each tracked UA through the robots.txt at site root."""
    per_ua: list[AiBotVerdict] = field(default_factory=list)
    rsl_license_urls: list[str] = field(default_factory=list)

    @property
    def blocked_count(self) -> int:
        return sum(1 for v in self.per_ua if v.status == "disallowed")

    @property
    def has_rsl(self) -> bool:
        return bool(self.rsl_license_urls)


def analyse_robots_for_ai(robots_text: str, site_url: str) -> RobotsAiAnalysis:
    """Run each tracked UA against the robots.txt at the site root.

    "Disallowed" means Protego reports the UA cannot fetch the site root URL.
    Anything more nuanced (e.g. only some paths disallowed) collapses to
    "allowed" here — we care about the headline opt-out signal, not a full
    path-by-path picture.

    RSL detection scans for ``License: <url>`` lines in the raw text. Protego
    doesn't surface unknown directives, so we parse the text directly.
    """
    out = RobotsAiAnalysis()
    if not robots_text:
        # No robots.txt fetched — treat every tracked UA as "allowed" since
        # there is no explicit rule against them. This matches what crawlers
        # actually do: absent robots.txt is permissive.
        for u in KNOWN_UAS:
            out.per_ua.append(AiBotVerdict(ua=u.ua, operator=u.operator, status="allowed"))
        return out

    parser = Protego.parse(robots_text)
    root = site_url if site_url.endswith("/") else site_url + "/"
    for u in KNOWN_UAS:
        allowed = parser.can_fetch(root, u.ua)
        out.per_ua.append(
            AiBotVerdict(ua=u.ua, operator=u.operator, status="allowed" if allowed else "disallowed")
        )

    out.rsl_license_urls = [m.group(1).strip() for m in _RSL_LINE_RE.finditer(robots_text)]
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Site-wide opt-out file probes
# ──────────────────────────────────────────────────────────────────────────────

# Match trust.txt `datatrainingallowed=<value>` directive. Spec:
# https://journallist.net/trust-txt-specification ; the AI-training extension
# is widely understood as `datatrainingallowed=no` to opt out.
_TRUST_DTA_RE = re.compile(r"^\s*datatrainingallowed\s*=\s*(\S+)", re.IGNORECASE | re.MULTILINE)


@dataclass
class SiteOptoutSignals:
    """All site-wide AI opt-out probe results for a single site."""
    # Tdmrep
    has_tdmrep: bool = False
    tdmrep_url: str | None = None
    # ai.txt
    has_ai_txt: bool = False
    ai_txt_url: str | None = None
    # trust.txt
    has_trust_txt: bool = False
    trust_txt_url: str | None = None
    trust_txt_datatraining: str | None = None  # the value, e.g. "no" / "yes" — None if directive absent
    # RSL — populated from robots.txt analysis, mirrored here for convenience
    has_rsl: bool = False
    rsl_license_urls: list[str] = field(default_factory=list)
    # robots.txt AI-bot matrix
    robots_ai: RobotsAiAnalysis = field(default_factory=RobotsAiAnalysis)


async def _probe_exists(client: httpx.AsyncClient, url: str) -> bool:
    """Return True if a GET on `url` succeeds with 2xx and a non-empty body.

    We use GET rather than HEAD because some misconfigured servers reject HEAD
    or strip the body byte count; a small GET is more reliable. Body size is
    capped implicitly by the per-request timeout — these files are spec'd to
    be tiny, so any large response is treated as non-conformant by the caller.
    """
    try:
        r = await client.get(url, timeout=10.0, follow_redirects=True)
    except Exception:
        return False
    return 200 <= r.status_code < 300 and bool(r.content)


async def _fetch_text(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        r = await client.get(url, timeout=10.0, follow_redirects=True)
    except Exception:
        return None
    if not (200 <= r.status_code < 300) or not r.content:
        return None
    return r.text


async def probe_site_optouts(
    client: httpx.AsyncClient,
    site_url: str,
    robots_text: str,
) -> SiteOptoutSignals:
    """Run every site-wide probe for a single site.

    Caller is expected to have already fetched `robots_text` during discovery;
    we re-use it to extract the AI-bot matrix and the RSL `License:` line.
    The remaining probes are independent HTTP GETs of well-known URLs.

    The probes here are intentionally cheap: presence + minimal payload only.
    Full parsing of tdmrep.json policy URLs etc. is deferred to a follow-up.
    """
    out = SiteOptoutSignals()
    out.robots_ai = analyse_robots_for_ai(robots_text, site_url)
    out.has_rsl = out.robots_ai.has_rsl
    out.rsl_license_urls = out.robots_ai.rsl_license_urls

    # tdmrep — TDM Reservation Protocol. Lives at /.well-known/tdmrep.json.
    tdmrep_url = urljoin(site_url.rstrip("/") + "/", ".well-known/tdmrep.json")
    if await _probe_exists(client, tdmrep_url):
        out.has_tdmrep = True
        out.tdmrep_url = tdmrep_url

    # ai.txt — Spawning's proposal. Lives at /ai.txt.
    ai_txt_url = urljoin(site_url.rstrip("/") + "/", "ai.txt")
    if await _probe_exists(client, ai_txt_url):
        out.has_ai_txt = True
        out.ai_txt_url = ai_txt_url

    # trust.txt — JournalList's site-identity file. Spec allows either
    # /.well-known/trust.txt (recommended) or /trust.txt (legacy). Try both.
    for candidate in (
        urljoin(site_url.rstrip("/") + "/", ".well-known/trust.txt"),
        urljoin(site_url.rstrip("/") + "/", "trust.txt"),
    ):
        text = await _fetch_text(client, candidate)
        if text is None:
            continue
        out.has_trust_txt = True
        out.trust_txt_url = candidate
        m = _TRUST_DTA_RE.search(text)
        if m:
            out.trust_txt_datatraining = m.group(1).strip().lower()
        break

    return out


# ──────────────────────────────────────────────────────────────────────────────
# Image / article level helpers (used by images.py / articles.py)
# ──────────────────────────────────────────────────────────────────────────────

# noai/noimageai/noml: informal but in-the-wild AI-opt-out directives expressed
# via X-Robots-Tag response headers or <meta name="robots"> tags. Token list
# kept conservative — we only count tokens that are unambiguously about AI
# training; generic `noindex`/`nofollow` are not opt-outs in this sense.
_AI_ROBOTS_TOKENS = ("noai", "noimageai", "noml")


def scan_robots_directives(value: str | None) -> list[str]:
    """Return the AI-opt-out tokens present in a robots-style directive string.

    Accepts the value of an ``X-Robots-Tag`` header or a ``<meta name=\"robots\"
    content=\"...\">`` attribute. Tokens are comma-separated and may have
    ``unavailable_after:`` or bot-specific prefixes (e.g.
    ``googlebot: noai, noimageai``); we tokenise loosely and match against
    a known list.
    """
    if not value:
        return []
    found: list[str] = []
    seen: set[str] = set()
    # Split on commas first, then on whitespace to catch `bot: token` forms.
    for chunk in value.split(","):
        for token in chunk.strip().split():
            t = token.strip().strip(";").lower()
            if t in _AI_ROBOTS_TOKENS and t not in seen:
                seen.add(t)
                found.append(t)
    return found


# CAWG training-and-data-mining assertion in a C2PA manifest.
# Spec: https://cawg.io/training-and-data-mining/1.1/
# Assertion label is `cawg.training-mining` (some early implementations use
# `cawg.training-and-data-mining`; we match both prefixes).
_CAWG_ASSERTION_LABEL_PREFIXES = ("cawg.training-mining", "cawg.training-and-data-mining")


def extract_cawg_training_mining(manifest: dict | None) -> dict | None:
    """Return the data dict of a CAWG training-and-data-mining assertion, if present.

    Returns None when no such assertion exists in the active manifest. Returns
    the raw `data` dict otherwise so the caller can record both presence and
    the specific entries (e.g. {"cawg.ai_generative_training": {"use": "notAllowed"}}).
    """
    if not manifest:
        return None
    assertions = manifest.get("assertions") or []
    for a in assertions:
        label = (a or {}).get("label", "")
        if not isinstance(label, str):
            continue
        if any(label.startswith(p) for p in _CAWG_ASSERTION_LABEL_PREFIXES):
            data = a.get("data")
            return data if isinstance(data, dict) else {}
    return None
