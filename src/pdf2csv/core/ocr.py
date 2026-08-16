"""RapidOCR wrapper: lazy, offline, and safe to import when OCR is missing.

Two properties matter more than anything clever this module could do.

**It must import cleanly when the OCR extra is absent.** A deployment that only
sees digital PDFs should not carry 130 MB of inference runtime, and the app
must start and work normally without it. Every heavy import is therefore
deferred, and the absence is reported as a plain-English message rather than an
ImportError traceback in front of an analyst.

**It must never reach for the network.** The ONNX weights ship inside the
``rapidocr-onnxruntime`` wheel — detection 4.7 MB, recognition 10.9 MB,
orientation 0.6 MB — so nothing is downloaded on first run. This is checked at
startup rather than assumed, because a cached model on a developer machine
hides a first-run download perfectly and the failure then appears only on the
client's air-gapped desktop.
"""

from __future__ import annotations

import threading
from typing import Any

from pdf2csv.logging_setup import get_logger
from pdf2csv.models import TextBox

log = get_logger(__name__)

_engine: Any = None
_engine_lock = threading.Lock()
_unavailable_reason: str | None = None


def is_available() -> bool:
    """Can scanned pages be processed in this installation?"""
    return unavailable_reason() is None


def unavailable_reason() -> str | None:
    """``None`` when OCR works, otherwise a sentence an analyst can act on."""
    global _unavailable_reason
    if _unavailable_reason is not None:
        return _unavailable_reason or None

    missing: list[str] = []
    for module, package in (
        ("rapidocr_onnxruntime", "rapidocr-onnxruntime"),
        ("cv2", "opencv-python-headless"),
        ("onnxruntime", "onnxruntime"),
    ):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)

    if missing:
        _unavailable_reason = (
            "This copy was installed without the OCR add-on, so scanned pages "
            f"cannot be read. Missing: {', '.join(missing)}."
        )
    else:
        _unavailable_reason = ""
    return _unavailable_reason or None


def get_engine() -> Any:
    """Build the OCR engine once per process and reuse it.

    Model loading costs about half a second and the engine is thread-safe for
    inference, so a single shared instance is both faster and lighter than one
    per job. The double-checked lock keeps two simultaneous uploads from each
    building their own.
    """
    global _engine
    if _engine is not None:
        return _engine

    with _engine_lock:
        if _engine is not None:
            return _engine

        reason = unavailable_reason()
        if reason:
            raise RuntimeError(reason)

        from rapidocr_onnxruntime import RapidOCR

        log.info("loading OCR models")
        _engine = RapidOCR()
        log.info("OCR models ready")
        return _engine


def model_report() -> dict[str, Any]:
    """Where the weights live and how big they are — for the startup log.

    Proves the offline claim rather than asserting it: if this lists the files
    and their sizes, they are on disk and no download will happen.
    """
    reason = unavailable_reason()
    if reason:
        return {"available": False, "reason": reason, "models": []}

    from pathlib import Path

    import rapidocr_onnxruntime

    root = Path(rapidocr_onnxruntime.__file__).parent
    models = [
        {"name": p.name, "size_mb": round(p.stat().st_size / 1e6, 1)}
        for p in sorted(root.rglob("*.onnx"))
    ]
    return {"available": True, "reason": None, "root": str(root), "models": models}


def recognise(image: Any) -> list[TextBox]:
    """Run OCR over a whole page image and return positioned text.

    One pass over the full page — never per cell. Cropping each cell and
    OCR-ing it individually is the single most expensive mistake available in
    this pipeline: it multiplies runtime by roughly ten and produces *worse*
    results, because the recogniser loses the surrounding context it uses to
    disambiguate digits.
    """
    engine = get_engine()
    raw, _elapsed = engine(image)

    boxes: list[TextBox] = []
    for entry in raw or []:
        try:
            polygon, text, confidence = entry[0], entry[1], entry[2]
            xs = [float(point[0]) for point in polygon]
            ys = [float(point[1]) for point in polygon]
        except (TypeError, IndexError, ValueError):
            continue  # a malformed detection is not worth aborting a page for
        if not str(text).strip():
            continue
        boxes.append(
            TextBox(
                text=str(text).strip(),
                x0=min(xs),
                y0=min(ys),
                x1=max(xs),
                y1=max(ys),
                confidence=float(confidence),
            )
        )
    return boxes
