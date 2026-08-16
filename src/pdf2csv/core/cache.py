"""On-disk cache for OCR results, keyed by file content.

OCR is the only genuinely expensive step in the pipeline — tens of seconds a
page against milliseconds for everything else. It is also perfectly
deterministic for a given page and DPI, which makes it ideal to cache.

Caching *OCR output* rather than final results is the deliberate choice. During
development you re-run the same scanned document dozens of times while tuning a
profile or a validation rule; caching the finished CSV would invalidate on
every change, while caching the recognised text boxes survives all of it. A
25-minute document becomes a 3-second one for every run after the first.

The key is the file's SHA-256, so an edited PDF misses the cache automatically
and there is no staleness to reason about.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from pdf2csv.config import get_settings
from pdf2csv.logging_setup import get_logger
from pdf2csv.models import TextBox

log = get_logger(__name__)

_CACHE_VERSION = 1
"""Bump when the stored shape changes, to invalidate every existing entry."""


def file_sha256(path: str | Path, *, chunk_size: int = 1 << 20) -> str:
    """Content hash of a file, read in chunks so large PDFs stay cheap."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _entry_path(sha256: str, page_index: int, dpi: int) -> Path:
    settings = get_settings()
    # Shard by the first two hex characters: a flat directory with thousands of
    # files is slow to enumerate on Windows.
    return (
        settings.cache_dir
        / "ocr"
        / sha256[:2]
        / f"{sha256}.p{page_index}.d{dpi}.v{_CACHE_VERSION}.json"
    )


def load_page_scan(sha256: str, page_index: int, dpi: int) -> dict | None:
    """Return everything cached about one scanned page, or ``None`` on a miss.

    The whole page scan is cached — recognised text, detected ruling lines, page
    dimensions — not just the OCR boxes. That means a cache hit skips
    rasterising as well as recognition, which is the difference between a
    re-run costing three seconds and costing forty.

    Every failure mode — missing file, corrupt JSON, unreadable directory — is
    a cache miss, never an error. A broken cache must degrade to slowness, not
    to a failed extraction on a client desktop.
    """
    if not get_settings().cache_enabled:
        return None

    path = _entry_path(sha256, page_index, dpi)
    try:
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != _CACHE_VERSION:
            return None
        payload["boxes"] = [
            TextBox(
                text=item["text"],
                x0=item["bbox"][0],
                y0=item["bbox"][1],
                x1=item["bbox"][2],
                y1=item["bbox"][3],
                confidence=item.get("confidence", 1.0),
            )
            for item in payload.get("boxes", [])
        ]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.debug("cache miss for page %d (%s)", page_index, exc)
        return None
    return payload


def save_page_scan(sha256: str, page_index: int, dpi: int, payload: dict) -> None:
    """Store a page scan. Silent on failure — the cache is strictly optional."""
    if not get_settings().cache_enabled:
        return

    path = _entry_path(sha256, page_index, dpi)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        serialisable = dict(payload)
        serialisable["version"] = _CACHE_VERSION
        serialisable["boxes"] = [b.to_dict() for b in payload.get("boxes", [])]
        # Write to a temporary file and replace, so an interrupted run cannot
        # leave a half-written entry that later reads as corrupt.
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(serialisable), encoding="utf-8")
        temporary.replace(path)
    except (OSError, TypeError, ValueError) as exc:
        log.debug("could not write cache entry for page %d (%s)", page_index, exc)


def clear() -> int:
    """Delete the whole cache. Returns bytes reclaimed."""
    cache_root = get_settings().cache_dir / "ocr"
    if not cache_root.exists():
        return 0
    total = sum(f.stat().st_size for f in cache_root.rglob("*") if f.is_file())
    shutil.rmtree(cache_root, ignore_errors=True)
    log.info("cleared OCR cache (%.1f MB)", total / 1e6)
    return total


def size_bytes() -> int:
    cache_root = get_settings().cache_dir / "ocr"
    if not cache_root.exists():
        return 0
    return sum(f.stat().st_size for f in cache_root.rglob("*") if f.is_file())
