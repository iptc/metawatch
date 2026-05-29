"""AI opt-out signal probes.

Phase 3 work. Collects machine-readable opt-out signals across the eight
mechanisms tracked on the /ai-policy/ page:

  Site-wide  -- robots.txt AI-bot matrix, /.well-known/tdmrep.json, /ai.txt,
                robots.txt RSL `License:` directive, /.well-known/trust.txt
                (datatrainingallowed=).
  Image-level - noai/noimageai in HTTP response headers or article <meta robots>,
                CAWG Training and Data Mining Assertion in C2PA manifests, and
                the IPTC PLUS:DataMining XMP field (already extracted upstream).

This module owns the site-wide signals only. Image-level signals are detected
inline in articles.py / images.py and surfaced on the ImageRow.
"""

from __future__ import annotations

import json as _json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlunparse

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

# Match `Content-Signal: ...` lines (Cloudflare Content Signals).
# Spec: https://contentsignals.org/ — line value is a comma-separated list of
# `<signal>=<yes|no>` pairs, where <signal> ∈ {search, ai-input, ai-train}.
# The line may be scoped to a preceding User-agent group; we parse it
# text-wide rather than per-UA at this stage (mirroring how we treat RSL),
# because the headline question is presence + the publisher's stated stance.
_CONTENT_SIGNAL_LINE_RE = re.compile(
    r"^\s*Content-Signal\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE
)
# Recognised signal names from the Content Signals spec. Anything else we see
# on a line is preserved but doesn't get its own dashboard column.
_CONTENT_SIGNAL_KNOWN = ("search", "ai-input", "ai-train")


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
    # Cloudflare Content Signals — merged signal→value (last-wins across all
    # Content-Signal lines in the file). Keys are the lower-case signal names
    # (e.g. "ai-train"); values are the literal directive value as-written
    # (e.g. "no" / "yes"), lower-cased and stripped.
    content_signals: dict[str, str] = field(default_factory=dict)

    @property
    def blocked_count(self) -> int:
        return sum(1 for v in self.per_ua if v.status == "disallowed")

    @property
    def has_rsl(self) -> bool:
        return bool(self.rsl_license_urls)

    @property
    def has_content_signals(self) -> bool:
        return bool(self.content_signals)


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
    out.content_signals = _parse_content_signals(robots_text)
    return out


def _parse_content_signals(robots_text: str) -> dict[str, str]:
    """Extract Cloudflare Content-Signal directives from robots.txt.

    Returns a merged ``{signal: value}`` dict across every ``Content-Signal:``
    line found. Conflicts resolve last-line-wins; ordering matches the spec's
    "later directive overrides earlier" convention. Unknown signal names are
    preserved so a future spec extension doesn't silently drop data.
    """
    merged: dict[str, str] = {}
    for line_match in _CONTENT_SIGNAL_LINE_RE.finditer(robots_text):
        body = line_match.group(1)
        # Strip trailing inline comments; robots.txt allows `# comment` on any line.
        body = body.split("#", 1)[0]
        for pair in body.split(","):
            if "=" not in pair:
                continue
            name, _, value = pair.partition("=")
            name = name.strip().lower()
            value = value.strip().lower()
            if name and value:
                merged[name] = value
    return merged


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
    # Cloudflare Content Signals — also lifted from robots.txt analysis.
    # Spec: https://contentsignals.org/
    has_content_signals: bool = False
    content_signals: dict[str, str] = field(default_factory=dict)
    # robots.txt AI-bot matrix
    robots_ai: RobotsAiAnalysis = field(default_factory=RobotsAiAnalysis)


def site_root(site_url: str) -> str:
    """Return the scheme+host(+port) of a URL with a trailing slash, no path.

    Required because the probed files (tdmrep.json, ai.txt, trust.txt) are all
    host-level resources per their specs — RFC 8615 for /.well-known/. A naive
    urljoin against a configured site URL like ``https://news.yahoo.com/rss/``
    would build ``https://news.yahoo.com/rss/.well-known/tdmrep.json``, which
    not only points at the wrong place but on permissive servers (Yahoo's
    /rss/ catch-all routes everything to the RSS feed) returns a misleading
    200. Always strip the path.
    """
    p = urlparse(site_url)
    return urlunparse((p.scheme, p.netloc, "/", "", "", ""))


async def _probe_well_known(
    client: httpx.AsyncClient, url: str, *, expect: str,
) -> tuple[bool, str | None]:
    """Probe a well-known URL with a content-type sanity check.

    ``expect`` is "json" or "text". Returns (matched, text-or-none).

    The content-type check is needed because many servers respond 200 for
    unknown paths — single-page apps return the index HTML, catch-all
    rewrites (Yahoo's /rss/* → RSS feed) return whatever happened to be
    there. We reject HTML responses outright; for tdmrep.json we additionally
    require the body to parse as JSON.
    """
    try:
        r = await client.get(url, timeout=10.0, follow_redirects=True)
    except Exception:
        return False, None
    if not (200 <= r.status_code < 300) or not r.content:
        return False, None
    ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype.startswith("text/html") or ctype.endswith("/xml") or ctype.endswith("+xml"):
        return False, None
    if expect == "json":
        # Some servers return application/json or text/plain or even
        # application/octet-stream for .json files. Don't gate on content-type
        # alone — try to parse. If the body isn't JSON, it isn't tdmrep.json.
        try:
            _json.loads(r.text)
        except (ValueError, _json.JSONDecodeError):
            return False, None
    else:
        # text-expecting probes (ai.txt, trust.txt). Require a text/* MIME
        # or an obvious key=value/line-oriented body. Reject when the body
        # smells like HTML even with a permissive content-type.
        if ctype and not ctype.startswith("text/"):
            return False, None
        head = r.text.lstrip()[:200].lower()
        if head.startswith("<!doctype") or head.startswith("<html") or head.startswith("<?xml"):
            return False, None
    return True, r.text


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
    out.has_content_signals = out.robots_ai.has_content_signals
    out.content_signals = dict(out.robots_ai.content_signals)

    root = site_root(site_url)

    # tdmrep — TDM Reservation Protocol. Lives at /.well-known/tdmrep.json.
    tdmrep_url = root + ".well-known/tdmrep.json"
    matched, _ = await _probe_well_known(client, tdmrep_url, expect="json")
    if matched:
        out.has_tdmrep = True
        out.tdmrep_url = tdmrep_url

    # ai.txt — Spawning's proposal. Lives at /ai.txt.
    ai_txt_url = root + "ai.txt"
    matched, _ = await _probe_well_known(client, ai_txt_url, expect="text")
    if matched:
        out.has_ai_txt = True
        out.ai_txt_url = ai_txt_url

    # trust.txt — JournalList's site-identity file. Spec allows either
    # /.well-known/trust.txt (recommended) or /trust.txt (legacy). Try both.
    for candidate in (root + ".well-known/trust.txt", root + "trust.txt"):
        matched, text = await _probe_well_known(client, candidate, expect="text")
        if not matched or text is None:
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

# AI-relevant robots directives expressed via X-Robots-Tag response headers or
# <meta name="robots"> tags. Two flavours:
#
#   * AI-specific (unambiguous): `noai`, `noimageai`, `noml` — originated with
#     DeviantArt's 2022 policy.
#   * Cache/snippet directives that the IPTC Generative AI Opt-Out Best
#     Practices (v2.0, Rec 3) elevates to AI-opt-out status: `noarchive`
#     (interpreted by Bing/Copilot as no-AI-training) and `nosnippet` (per
#     Google's docs, also blocks use in AI Overviews/AI Mode). These overlap
#     with their classic search-snippet meaning, so a hit means "page uses a
#     directive the IPTC recommends for AI opt-out", not "definitely intended
#     as AI opt-out". Plain `noindex`/`nofollow` are still excluded as too
#     generic.
_AI_ROBOTS_TOKENS = ("noai", "noimageai", "noml", "noarchive", "nosnippet")


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


# CAWG Training and Data Mining Assertion in a C2PA manifest.
# Spec: https://cawg.io/training-and-data-mining/1.1/ (the URL slug is the
# only context in which the "training-and-data-mining" phrasing is correct;
# the assertion name proper is "Training and Data Mining" and its label is
# `cawg.training-mining`). Some early implementations used the legacy label
# `cawg.training-and-data-mining`; we match both prefixes.
_CAWG_ASSERTION_LABEL_PREFIXES = ("cawg.training-mining", "cawg.training-and-data-mining")


def extract_cawg_training_mining(manifest: dict | None) -> dict | None:
    """Return the data dict of a CAWG Training and Data Mining Assertion, if present.

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
