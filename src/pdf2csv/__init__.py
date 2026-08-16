"""PDF2CSV — validated table extraction from finance PDFs.

The public surface is deliberately tiny::

    from pdf2csv import run, export_result

    result = run("statement.pdf")
    export_result(result, "statement.csv")

Everything else — the CLI, the web UI, the notebook template, the tests — is a
thin caller of those two functions. No extraction logic lives anywhere else.

Imports here are lazy: pulling in :mod:`pandas` and :mod:`pdfplumber` costs
about a second, and ``python -m pdf2csv --help`` should not pay it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"
__all__ = ["ExtractionResult", "ValidationReport", "__version__", "export_result", "run"]

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from pdf2csv.core.export import export_result
    from pdf2csv.core.pipeline import run
    from pdf2csv.models import ExtractionResult, ValidationReport


def __getattr__(name: str) -> Any:
    """Resolve the public names on first use instead of at import time."""
    if name == "run":
        from pdf2csv.core.pipeline import run

        return run
    if name == "export_result":
        from pdf2csv.core.export import export_result

        return export_result
    if name in ("ExtractionResult", "ValidationReport"):
        from pdf2csv import models

        return getattr(models, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
