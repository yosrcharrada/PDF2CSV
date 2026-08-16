"""Rotating file logging, configured once at process start.

When this fails on a client desktop, the log file is the only forensic evidence
that exists — the analyst will not have kept the console window, and asking
them to reproduce a failure with a document they are not allowed to email you
is a dead end. So: log to a file by default, keep a week of history, and record
the environment on startup.

Deliberately never logs document *content*. Filenames, page counts and check
results are safe; cell values are client financial data and stay out of the log.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import sys
from pathlib import Path

from pdf2csv.config import get_settings

_CONFIGURED = False

_FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"
_CONSOLE_FORMAT = "%(levelname)-7s %(message)s"


class _ConsoleFilter(logging.Filter):
    """Keep the console readable; the file keeps everything."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(("pdfminer", "PIL", "uvicorn.access"))


def setup_logging(*, level: str | None = None, console: bool = True) -> Path | None:
    """Attach handlers to the root logger. Safe to call more than once.

    Returns the log file path, or ``None`` if no writable location was found —
    in which case console logging still works and the app still runs.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return _current_log_path()

    settings = get_settings()
    resolved_level = (level or settings.log_level).upper()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    log_path: Path | None = None
    try:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = settings.logs_dir / "pdf2csv.log"
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=2_000_000, backupCount=7, encoding="utf-8"
        )
        file_handler.setLevel(getattr(logging, resolved_level, logging.INFO))
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
        root.addHandler(file_handler)
    except OSError:
        log_path = None

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(getattr(logging, resolved_level, logging.INFO))
        stream.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
        stream.addFilter(_ConsoleFilter())
        root.addHandler(stream)

    # pdfminer logs one line per glyph at DEBUG. That is not useful to anyone.
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    logging.getLogger("pdfplumber").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)

    _CONFIGURED = True
    _log_environment(log_path)
    return log_path


def _current_log_path() -> Path | None:
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            return Path(handler.baseFilename)
    return None


def _log_environment(log_path: Path | None) -> None:
    """One block at startup that answers most 'what machine was this?' questions."""
    from pdf2csv import __version__

    settings = get_settings()
    log = logging.getLogger("pdf2csv.startup")
    log.info("-" * 72)
    log.info("PDF2CSV %s starting", __version__)
    log.info("python      %s (%s)", platform.python_version(), sys.executable)
    log.info("platform    %s", platform.platform())
    log.info("home        %s", settings.home)
    log.info("output      %s", settings.output_dir)
    log.info("models      %s", settings.models_dir)
    log.info("log file    %s", log_path or "<none — console only>")
    offline = [k for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE") if os.environ.get(k)]
    log.info("offline env %s", ", ".join(offline) or "<not set>")


def get_logger(name: str) -> logging.Logger:
    """Module-level logger helper, so callers never import logging directly."""
    return logging.getLogger(name)
