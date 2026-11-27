"""Remote C2PA manifests referenced from XMP dcterms:provenance.

Photoshop 28 (2026-11) attaches Content Credentials to generative edits
without embedding JUMBF: the file carries only an XMP ``dcterms:provenance``
URL pointing at cai-manifests.adobe.com. The crawler fetches that manifest
itself (bounded timeout and size) and records where the manifest lived.

The fixture ``photoshop-remote-manifest.c2pa`` is the real manifest store
Adobe served for a Photoshop "Generate Fill" test photo. The tiny PNG built
here is a different image, so validation reports a data-hash mismatch — what
matters for these tests is that the manifest is located, fetched and read.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import httpx
import pytest

from pmd_crawler import images

FIXTURE = Path(__file__).parent / "fixtures" / "photoshop-remote-manifest.c2pa"
IMAGE_URL = "https://news.example.test/photo.png"
MANIFEST_URL = "https://manifests.example.test/urn-c2pa-test"


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _png(provenance: str | None) -> bytes:
    """1x1 PNG, optionally with an XMP packet carrying dcterms:provenance."""
    out = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    if provenance:
        xmp = (
            '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
            'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            '<rdf:Description rdf:about="" xmlns:dcterms="http://purl.org/dc/terms/" '
            f'dcterms:provenance="{provenance}"/></rdf:RDF></x:xmpmeta>'
        )
        out += _chunk(b"iTXt", b"XML:com.adobe.xmp\x00\x00\x00\x00\x00" + xmp.encode())
    out += _chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\xff"))
    return out + _chunk(b"IEND", b"")


class _NoExif:
    async def read(self, path: Path) -> dict[str, object]:
        return {}


async def _analyse(png: bytes, manifest_handler) -> tuple[images.ImageResult, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == IMAGE_URL:
            return httpx.Response(200, content=png, headers={"content-type": "image/png"})
        return manifest_handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await images.fetch_and_analyse(client, IMAGE_URL, _NoExif())
    return result, seen


async def test_remote_manifest_is_fetched_and_read():
    result, seen = await _analyse(
        _png(MANIFEST_URL),
        lambda r: httpx.Response(200, content=FIXTURE.read_bytes(),
                                 headers={"content-type": "application/c2pa"}),
    )
    assert MANIFEST_URL in seen
    assert result.has_c2pa is True
    assert result.c2pa_manifest_location == "remote"
    assert result.c2pa_remote_manifest_url == MANIFEST_URL
    assert result.c2pa_remote_fetch_error is None
    assert result.c2pa_manifest_signer == "Adobe Inc."
    # Different pixels from the signed asset, so the hash can't match.
    assert "assertion.dataHash.mismatch" in result.c2pa_failure_codes


@pytest.mark.parametrize("handler, error", [
    (lambda r: httpx.Response(403), "http_403"),
    (lambda r: httpx.Response(404), "http_404"),
    (lambda r: httpx.Response(200, content=b"not a manifest"), "unreadable"),
    (lambda r: httpx.Response(200, content=b"x" * (images.MAX_REMOTE_MANIFEST_BYTES + 1)), "too_large"),
])
async def test_failed_remote_fetch_keeps_the_reference(handler, error):
    result, _ = await _analyse(_png(MANIFEST_URL), handler)
    assert result.has_c2pa is False
    assert result.c2pa_manifest_location is None
    assert result.c2pa_remote_manifest_url == MANIFEST_URL
    assert result.c2pa_remote_fetch_error == error


async def test_remote_fetch_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    result, _ = await _analyse(_png(MANIFEST_URL), handler)
    assert result.c2pa_remote_manifest_url == MANIFEST_URL
    assert result.c2pa_remote_fetch_error == "timeout"


async def test_non_https_reference_is_not_fetched():
    url = "http://manifests.example.test/plain"
    result, seen = await _analyse(_png(url), lambda r: httpx.Response(500))
    assert seen == [IMAGE_URL]
    assert result.c2pa_remote_manifest_url == url
    assert result.c2pa_remote_fetch_error == "not_https"


async def test_image_without_c2pa_makes_no_extra_request():
    result, seen = await _analyse(_png(None), lambda r: httpx.Response(500))
    assert seen == [IMAGE_URL]
    assert result.has_c2pa is False
    assert result.c2pa_remote_manifest_url is None
    assert result.c2pa_remote_fetch_error is None


def test_xmp_provenance_fallback_reads_exiftool_key():
    tags = {"XMP-dcterms:Provenance": " https://cai-manifests.adobe.com/manifests/x "}
    assert images._xmp_provenance(tags) == "https://cai-manifests.adobe.com/manifests/x"
    assert images._xmp_provenance({"XMP-dc:Title": "x"}) is None
