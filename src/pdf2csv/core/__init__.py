"""Extraction internals.

Nothing outside this package should import anything from it except
:func:`pdf2csv.core.pipeline.run` and :func:`pdf2csv.core.export.export_result`.
Keeping that boundary is what lets the CLI, the web UI and the notebook stay
interchangeable thin callers.
"""
