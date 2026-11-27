"""Image fetch + ExifTool analysis."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import c2pa
import exiftool
import httpx

from . import cdn, optout, scoring
from . import dst as dst_mod

MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MB
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/avif", "image/gif", "image/tiff"}

# Remote C2PA manifests (referenced from XMP dcterms:provenance rather than
# embedded as JUMBF — e.g. Photoshop's mandatory Content Credentials, 2026-11)
# are fetched by us through the crawler's own httpx client, NOT by c2pa-rs,
# so the request gets a bounded timeout and size cap. c2pa-rs is configured
# never to go to the network itself.
MAX_REMOTE_MANIFEST_BYTES = 2 * 1024 * 1024  # real Adobe manifests are ~30 KB
REMOTE_MANIFEST_TIMEOUT = httpx.Timeout(10.0)
_C2PA_SETTINGS = {"verify": {"remote_manifest_fetch": False}}


def _c2pa_context() -> c2pa.Context:
    # Fresh per call: readers run on executor threads, and contexts are cheap.
    return c2pa.Context(c2pa.Settings.from_dict(_C2PA_SETTINGS))

_REMOTE_URL_RE = re.compile(r"must fetch remote manifests from url (\S+)")


@dataclass
class ImageResult:
    image_url: str
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    file_size_bytes: int | None = None
    http_status: int = 0
    has_exif: bool = False
    has_iptc_iim: bool = False
    has_iptc_xmp: bool = False
    has_c2pa: bool = False
    c2pa_manifest_signer: str | None = None
    c2pa_validation_status: str | None = None
    c2pa_failure_codes: list[str] = field(default_factory=list)
    # "embedded" (JUMBF in the file) | "remote" (fetched via dcterms:provenance)
    # | None when no manifest was read.
    c2pa_manifest_location: str | None = None
    # Set whenever the image references a remote manifest, even if fetching
    # or reading it failed — so a dead link still shows up in the data.
    c2pa_remote_manifest_url: str | None = None
    # Why a referenced remote manifest wasn't read: "http_<status>",
    # "timeout", "network", "too_large", "not_https", "unreadable".
    c2pa_remote_fetch_error: str | None = None
    dst_iptc: str | None = None
    dst_c2pa: list[str] = field(default_factory=list)
    # digitalSourceType URIs from every manifest in the C2PA provenance tree
    # (active first), and the strongest AI level they imply: generated |
    # contains | algorithmic | none-declared; None when no manifest was read.
    dst_c2pa_tree: list[str] = field(default_factory=list)
    c2pa_ai_level: str | None = None
    cdn_provider: str = "unknown"
    cdn_optimizer_active: str = "unknown"
    metadata_field_count: int = 0
    iptc_score: float = 0.0
    per_field_presence: list[tuple[str, bool]] = field(default_factory=list)
    raw_tags: dict[str, object] = field(default_factory=dict)
    # AI-opt-out signals (Phase 3): noai/noimageai tokens harvested from this
    # image's X-Robots-Tag response header; CAWG Training and Data Mining
    # Assertion data dict (or None) if present in the C2PA manifest.
    noai_tokens: list[str] = field(default_factory=list)
    cawg_training_mining: dict | None = None


async def fetch_and_analyse(
    client: httpx.AsyncClient,
    image_url: str,
    exif_pool: ExifPool,
) -> ImageResult:
    result = ImageResult(image_url=image_url)

    try:
        async with client.stream("GET", image_url, timeout=15.0, follow_redirects=True) as r:
            result.http_status = r.status_code
            result.mime_type = r.headers.get("content-type", "").split(";")[0].strip() or None
            # Collect noai/noimageai tokens from the image's X-Robots-Tag header
            # (image-level AI opt-out signal). Header may repeat — sum across all.
            xrt_values = r.headers.get_list("x-robots-tag") if hasattr(r.headers, "get_list") else [
                v for k, v in r.headers.items() if k.lower() == "x-robots-tag"
            ]
            for hv in xrt_values:
                for tok in optout.scan_robots_directives(hv):
                    if tok not in result.noai_tokens:
                        result.noai_tokens.append(tok)
            content_length = r.headers.get("content-length")
            if content_length and content_length.isdigit() and int(content_length) > MAX_IMAGE_BYTES:
                cdn_info = cdn.detect(image_url, dict(r.headers))
                result.cdn_provider = cdn_info.provider
                result.cdn_optimizer_active = cdn_info.optimizer_active
                return result
            if result.mime_type and result.mime_type not in ALLOWED_MIME:
                cdn_info = cdn.detect(image_url, dict(r.headers))
                result.cdn_provider = cdn_info.provider
                result.cdn_optimizer_active = cdn_info.optimizer_active
                return result

            cdn_info = cdn.detect(image_url, dict(r.headers))
            result.cdn_provider = cdn_info.provider
            result.cdn_optimizer_active = cdn_info.optimizer_active

            if r.status_code >= 400:
                return result

            with tempfile.NamedTemporaryFile(delete=False, suffix=_suffix_for(result.mime_type)) as tmp:
                size = 0
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        tmp.close()
                        os.unlink(tmp.name)
                        return result
                    tmp.write(chunk)
                tmp_path = Path(tmp.name)
                result.file_size_bytes = size
    except Exception:
        return result

    try:
        tags = await exif_pool.read(tmp_path)
    except Exception:
        tags = {}

    # C2PA presence + active manifest summary. c2pa-rs is blocking, so it
    # runs in an executor. Any reader error means "no manifest" — there are
    # many legitimate ways for that to happen (wrong format, truncated file).
    loop = asyncio.get_running_loop()
    try:
        c2pa_res = await loop.run_in_executor(None, _detect_c2pa, tmp_path, result.mime_type, None)
        if c2pa_res.remote_url is None and not c2pa_res.present:
            c2pa_res.remote_url = _xmp_provenance(tags)
        if c2pa_res.remote_url and not c2pa_res.present:
            result.c2pa_remote_manifest_url = c2pa_res.remote_url
            manifest_bytes, err = await fetch_remote_manifest(client, c2pa_res.remote_url)
            if manifest_bytes is None:
                result.c2pa_remote_fetch_error = err
            else:
                c2pa_res = await loop.run_in_executor(
                    None, _detect_c2pa, tmp_path, result.mime_type, manifest_bytes
                )
                if not c2pa_res.present:
                    result.c2pa_remote_fetch_error = "unreadable"
        _apply_c2pa(result, c2pa_res)
    except Exception:
        pass

    try:
        os.unlink(tmp_path)
    except OSError:
        pass

    result.raw_tags = tags
    result.metadata_field_count = sum(1 for k in tags if not k.startswith("SourceFile") and not k.startswith("File:"))
    result.dst_iptc = dst_mod.extract_dst_from_xmp(tags)

    has_exif, has_iptc, has_xmp = scoring.families_present(tags)
    result.has_exif = has_exif
    result.has_iptc_iim = has_iptc
    result.has_iptc_xmp = has_xmp

    dims = _extract_dimensions(tags)
    if dims:
        result.width, result.height = dims

    score, presence = scoring.score_image(tags)
    result.iptc_score = score
    result.per_field_presence = presence
    return result


@dataclass
class C2paRead:
    present: bool = False
    location: str | None = None  # "embedded" | "remote"
    remote_url: str | None = None
    signer: str | None = None
    state: str | None = None
    failure_codes: list[str] = field(default_factory=list)
    dst_uris: list[str] = field(default_factory=list)
    dst_tree_uris: list[str] = field(default_factory=list)
    ai_level: str | None = None
    cawg: dict | None = None


def _apply_c2pa(result: ImageResult, r: C2paRead) -> None:
    result.has_c2pa = r.present
    result.c2pa_manifest_location = r.location
    if r.remote_url:
        result.c2pa_remote_manifest_url = r.remote_url
    result.c2pa_manifest_signer = r.signer
    result.c2pa_validation_status = r.state
    result.c2pa_failure_codes = r.failure_codes
    result.dst_c2pa = r.dst_uris
    result.dst_c2pa_tree = r.dst_tree_uris
    result.c2pa_ai_level = r.ai_level
    result.cawg_training_mining = r.cawg


def _xmp_provenance(tags: dict[str, object]) -> str | None:
    """dcterms:provenance from ExifTool output (-G1 key ``XMP-dcterms:Provenance``)."""
    for k, v in tags.items():
        if k.lower().endswith("xmp-dcterms:provenance") and isinstance(v, str) and v.strip():
            return v.strip()
    return None


async def fetch_remote_manifest(
    client: httpx.AsyncClient, url: str
) -> tuple[bytes | None, str | None]:
    """Fetch a remote C2PA manifest store. Returns (bytes, None) or (None, error_code)."""
    if not url.lower().startswith("https://"):
        return None, "not_https"
    try:
        async with client.stream(
            "GET", url, timeout=REMOTE_MANIFEST_TIMEOUT, follow_redirects=True
        ) as r:
            if r.status_code >= 400:
                return None, f"http_{r.status_code}"
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_REMOTE_MANIFEST_BYTES:
                    return None, "too_large"
            return bytes(buf), None
    except httpx.TimeoutException:
        return None, "timeout"
    except Exception:
        return None, "network"


def _detect_c2pa(
    path: Path, mime: str | None = None, manifest_data: bytes | None = None
) -> C2paRead:
    """Read the C2PA manifest for *path*.

    Detection is presence-first: c2pa-rs raises ``ManifestNotFound`` when no
    manifest is present, and various parse errors for malformed or non-image
    content. Any exception is treated as "no manifest" — false negatives are
    acceptable, false positives would be misleading.

    c2pa-rs never fetches over the network here. When the file only carries
    a remote reference (XMP dcterms:provenance), the reader raises a
    "must fetch remote manifests from url …" error; we return that URL in
    ``remote_url`` and the caller fetches it and calls again with
    ``manifest_data``.

    failure_codes is the list of `failure[].code` entries from the active
    manifest's validation results — e.g. ``signingCredential.untrusted``,
    ``assertion.dataHash.mismatch``. Empty for valid manifests.

    dst_uris collects every distinct digitalSourceType URI seen in the
    active manifest's c2pa.actions / c2pa.actions.v2 assertions;
    dst_tree_uris and ai_level cover the whole provenance tree (see
    dst.collect_c2pa_ai_disclosure).
    """
    try:
        if manifest_data is not None:
            with open(path, "rb") as f:
                reader = c2pa.Reader(
                    mime or path.suffix.lstrip(".") or "application/octet-stream", f,
                    manifest_data=manifest_data, context=_c2pa_context(),
                )
        else:
            reader = c2pa.Reader(str(path), context=_c2pa_context())
    except Exception as e:
        m = _REMOTE_URL_RE.search(str(e))
        return C2paRead(remote_url=m.group(1) if m else None)
    out = C2paRead(present=True, location="remote" if manifest_data is not None else "embedded")
    try:
        state = reader.get_validation_state()
        out.state = str(state) if state is not None else None
        manifest = reader.get_active_manifest()
        if isinstance(manifest, dict):
            sig = manifest.get("signature_info")
            if isinstance(sig, dict):
                out.signer = sig.get("issuer") or sig.get("signer")
        try:
            vr = reader.get_validation_results()
            if isinstance(vr, dict):
                active = vr.get("activeManifest") or {}
                out.failure_codes = [f["code"] for f in active.get("failure", []) if "code" in f]
        except Exception:
            pass
        out.dst_uris = dst_mod.extract_dst_from_c2pa_manifest(manifest)
        out.ai_level = "none-declared"
        try:
            store = json.loads(reader.json())
            out.ai_level, out.dst_tree_uris = dst_mod.collect_c2pa_ai_disclosure(store)
        except Exception:
            out.dst_tree_uris = list(out.dst_uris)
        out.cawg = optout.extract_cawg_training_mining(manifest) if isinstance(manifest, dict) else None
    except Exception:
        pass
    return out


def _suffix_for(mime: str | None) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/avif": ".avif",
        "image/gif": ".gif",
        "image/tiff": ".tif",
    }.get(mime or "", ".bin")


def _extract_dimensions(tags: dict[str, object]) -> tuple[int, int] | None:
    width = None
    height = None
    for k, v in tags.items():
        kl = k.lower()
        if kl.endswith(":imagewidth") or kl == "imagewidth":
            try:
                width = int(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass
        if kl.endswith(":imageheight") or kl == "imageheight":
            try:
                height = int(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass
    if width and height:
        return width, height
    return None


class ExifPool:
    """Small wrapper around pyexiftool that runs ExifTool in batch mode.

    ExifTool startup is slow (~150ms). Reusing one long-running process is the
    standard performance idiom and is what pyexiftool's `ExifToolHelper` provides.
    """

    def __init__(self) -> None:
        self._helper: exiftool.ExifToolHelper | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> ExifPool:
        # `-G1` is the family-1 group prefix (e.g. XMP-dc:, XMP-iptcExt:,
        # IFD0:) and is far more informative than the flat `-G` family-0
        # prefix (XMP:, EXIF:). MUST be passed via common_args at construction
        # — ExifToolHelper's default common_args is `["-G", "-n"]`, which is
        # baked into the persistent exiftool process at startup. Passing
        # `params=["-G1"]` per call doesn't override it: exiftool resolves
        # `-G ... -G1` to combined output (XMP:XMP-dc:Description) only when
        # both stick, and in pyexiftool's persistent-mode invocation the
        # startup `-G` wins on its own, producing `XMP:` keys with no
        # family-1 information at all. `-n` disables print conversion
        # (numeric values preserved as numbers).
        self._helper = exiftool.ExifToolHelper(common_args=["-G1", "-n"])
        self._helper.run()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._helper is not None:
            self._helper.terminate()
            self._helper = None

    async def read(self, path: Path) -> dict[str, object]:
        assert self._helper is not None
        async with self._lock:
            data = await asyncio.get_running_loop().run_in_executor(
                None,
                # Group/print-conversion flags are set on the persistent
                # exiftool process via common_args in __aenter__ — see the
                # comment there for why per-call `params` would not work.
                lambda: self._helper.get_metadata(str(path)),  # type: ignore[union-attr]
            )
        if not data:
            return {}
        return data[0]
