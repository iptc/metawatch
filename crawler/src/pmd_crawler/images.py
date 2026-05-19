"""Image fetch + ExifTool analysis."""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import c2pa
import exiftool
import httpx

from . import cdn, scoring
from . import dst as dst_mod

MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MB
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/avif", "image/gif", "image/tiff"}


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
    dst_iptc: str | None = None
    dst_c2pa: list[str] = field(default_factory=list)
    cdn_provider: str = "unknown"
    cdn_optimizer_active: str = "unknown"
    metadata_field_count: int = 0
    iptc_score: float = 0.0
    per_field_presence: list[tuple[str, bool]] = field(default_factory=list)
    raw_tags: dict[str, object] = field(default_factory=dict)


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

    # C2PA presence + active manifest summary. Runs in an executor because the
    # c2pa-rs binding is blocking. Any error means "no manifest" — there are many
    # legitimate ways for that to happen (wrong format, truncated file, etc.).
    try:
        c2pa_present, c2pa_signer, c2pa_state, c2pa_failures, c2pa_dst = (
            await asyncio.get_running_loop().run_in_executor(None, _detect_c2pa, tmp_path)
        )
        result.has_c2pa = c2pa_present
        result.c2pa_manifest_signer = c2pa_signer
        result.c2pa_validation_status = c2pa_state
        result.c2pa_failure_codes = c2pa_failures
        result.dst_c2pa = c2pa_dst
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


def _detect_c2pa(
    path: Path,
) -> tuple[bool, str | None, str | None, list[str], list[str]]:
    """Return (has_c2pa, signer_issuer, validation_state, failure_codes, dst_uris).

    Detection is presence-first: the c2pa-rs Python binding raises
    ``ManifestNotFound`` when no JUMBF manifest is embedded, and various
    other parse errors for malformed or non-image content. Any exception is
    treated as "no manifest" — false negatives are acceptable, false positives
    would be misleading.

    failure_codes is the list of `failure[].code` entries from the active
    manifest's validation results — e.g. ``signingCredential.untrusted``,
    ``assertion.dataHash.mismatch``. Empty for valid manifests.

    dst_uris collects every distinct digitalSourceType URI seen in the
    active manifest's c2pa.actions / c2pa.actions.v2 assertions.
    """
    try:
        reader = c2pa.Reader(str(path))
    except Exception:
        return False, None, None, [], []
    try:
        state = reader.get_validation_state()
        manifest = reader.get_active_manifest()
        signer = None
        if isinstance(manifest, dict):
            sig = manifest.get("signature_info")
            if isinstance(sig, dict):
                signer = sig.get("issuer") or sig.get("signer")
        failure_codes: list[str] = []
        try:
            vr = reader.get_validation_results()
            if isinstance(vr, dict):
                active = vr.get("activeManifest") or {}
                failure_codes = [f["code"] for f in active.get("failure", []) if "code" in f]
        except Exception:
            pass
        dst_uris = dst_mod.extract_dst_from_c2pa_manifest(manifest)
        return True, signer, str(state) if state is not None else None, failure_codes, dst_uris
    except Exception:
        return True, None, None, [], []


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
        self._helper = exiftool.ExifToolHelper()
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
                # -G1 (family-1 group prefix): produces "XMP-dc:Creator" /
                # "XMP-iptcExt:DigitalSourceType" / "IFD0:Artist" etc. instead
                # of the flat -G "XMP:Creator". Far more informative for the
                # per-image metadata table and matches the namespace-qualified
                # aliases in scoring.yaml.
                lambda: self._helper.get_metadata(str(path), params=["-G1", "-n"]),  # type: ignore[union-attr]
            )
        if not data:
            return {}
        return data[0]
