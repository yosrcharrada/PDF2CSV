"""Background job runner for the web UI.

Extraction takes anywhere from 200 milliseconds to several minutes depending on
how many pages need OCR. A request that blocks for four minutes looks
identical to a crash from the browser, so uploads return immediately with a job
id and the work happens on a worker thread.

The design constraint that shapes everything here: **this is one analyst on one
desktop, not a service.** So jobs live in memory, concurrency is capped at two,
and there is no database. What it does need is honest progress — a progress bar
that sits at "processing…" for four minutes teaches the analyst that the tool
has hung, and the next thing they do is kill it halfway through a document.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from pdf2csv.config import get_settings
from pdf2csv.core.export import ExportPaths, export_result
from pdf2csv.logging_setup import get_logger
from pdf2csv.models import ExtractionResult

log = get_logger(__name__)

MAX_WORKERS = 2
"""Two, not more. OCR is CPU-bound and saturates the cores it is given; running
four jobs makes all four slow rather than any of them fast, and the analyst is
watching one of them."""

PREVIEW_ROWS = 500
"""Rows sent to the browser. The full table goes in the download — rendering a
20,000-row grid janks the page and nobody scrolls it."""


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


# Stage weights for the progress bar. OCR dominates real documents, so it gets
# most of the range; the rest would otherwise crawl to 90% and then sit there.
_STAGE_SPAN: dict[str, tuple[float, float]] = {
    "reading": (0.0, 0.03),
    "routing": (0.03, 0.08),
    "digital": (0.08, 0.30),
    "ocr": (0.30, 0.92),
    "assembling": (0.92, 0.95),
    "validating": (0.95, 0.99),
    "done": (1.0, 1.0),
}


@dataclass
class ProgressEvent:
    """One tick, as the browser sees it."""

    sequence: int
    stage: str
    message: str
    percent: float
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "stage": self.stage,
            "message": self.message,
            "percent": round(self.percent, 4),
        }


@dataclass
class Job:
    """One uploaded document and everything that happened to it."""

    id: str
    filename: str
    size_bytes: int
    upload_path: Path
    output_dir: Path
    status: JobStatus = JobStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None

    result: ExtractionResult | None = None
    exports: ExportPaths | None = None
    events: list[ProgressEvent] = field(default_factory=list)

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- progress ------------------------------------------------------------
    def record(self, stage: str, current: int, total: int, message: str) -> None:
        low, high = _STAGE_SPAN.get(stage, (0.0, 1.0))
        fraction = (current / total) if total else 0.0
        percent = low + (high - low) * min(max(fraction, 0.0), 1.0)

        with self._lock:
            # Never let the bar go backwards. Stages can report out of order
            # when a page reroutes from digital to OCR, and a bar that jumps
            # back reads as "it is starting again".
            if self.events:
                percent = max(percent, self.events[-1].percent)
            self.events.append(
                ProgressEvent(
                    sequence=len(self.events),
                    stage=stage,
                    message=message,
                    percent=percent,
                )
            )

    def events_since(self, sequence: int) -> list[ProgressEvent]:
        with self._lock:
            return [e for e in self.events if e.sequence >= sequence]

    @property
    def percent(self) -> float:
        with self._lock:
            return self.events[-1].percent if self.events else 0.0

    @property
    def stage_message(self) -> str:
        with self._lock:
            return self.events[-1].message if self.events else "Waiting to start"

    @property
    def duration(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    @property
    def is_finished(self) -> bool:
        return self.status in (JobStatus.DONE, JobStatus.FAILED)

    # -- serialisation -------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """Everything the UI needs to render a job card."""
        data: dict[str, Any] = {
            "id": self.id,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "status": self.status.value,
            "percent": round(self.percent, 4),
            "message": self.stage_message,
            "created_at": self.created_at,
            "duration": round(self.duration, 2),
            "error": self.error,
        }

        if self.result is not None:
            report = self.result.report
            data["result"] = {
                "n_rows": self.result.n_rows,
                "columns": self.result.columns,
                "passed": report.passed,
                "summary": report.summary(),
                "checks": [c.to_dict() for c in report.checks],
                "flags": [f.to_dict() for f in report.flags],
                "document": self.result.meta.to_dict(),
                "extra_tables": len(self.result.extra_frames),
                "tables": [t.to_dict() for t in self.result.tables_out],
            }
            data["downloads"] = self.download_map()
            data["output_dir"] = str(self.output_dir)
        return data

    def download_map(self) -> dict[str, bool]:
        if self.exports is None:
            return {}
        return {
            "csv": self.exports.csv.is_file(),
            "xlsx": bool(self.exports.xlsx and self.exports.xlsx.is_file()),
            "json": self.exports.report_json.is_file(),
        }

    def table_count(self) -> int:
        return len(self.result.tables_out) if self.result else 0

    def frame_for(self, table: int):
        """The dataframe for a table index, or ``None`` if out of range."""
        if self.result is None:
            return None
        if self.result.tables_out and 0 <= table < len(self.result.tables_out):
            return self.result.tables_out[table].frame
        return self.result.dataframe if table == 0 else None

    def preview(
        self, limit: int = PREVIEW_ROWS, offset: int = 0, table: int = 0
    ) -> dict[str, Any]:
        """A JSON-safe slice of one table, with NaN rendered as null."""
        frame = self.frame_for(table)
        if frame is None:
            return {"columns": [], "rows": [], "total": 0, "offset": 0, "truncated": False}

        import pandas as pd

        window = frame.iloc[offset : offset + limit]

        columns = [str(c) for c in frame.columns]
        kinds = [
            "number" if pd.api.types.is_numeric_dtype(frame[c]) else "text"
            for c in frame.columns
        ]

        rows: list[list[Any]] = []
        for record in window.itertuples(index=False, name=None):
            rows.append([None if pd.isna(v) else _jsonable(v) for v in record])

        flags = []
        if self.result and self.result.tables_out and 0 <= table < len(self.result.tables_out):
            flags = [f.to_dict() for f in self.result.tables_out[table].report.flags]

        return {
            "table": table,
            "columns": columns,
            "kinds": kinds,
            "rows": rows,
            "flags": flags,
            "total": len(frame),
            "offset": offset,
            "truncated": len(frame) > offset + limit,
        }

    def cleanup(self) -> None:
        """Delete this job's uploaded PDF and its outputs."""
        # A file still held open by a virus scanner is not worth an exception;
        # the sweep runs again on the next upload.
        with contextlib.suppress(OSError):
            self.upload_path.unlink(missing_ok=True)
        shutil.rmtree(self.output_dir, ignore_errors=True)


def _jsonable(value: Any) -> Any:
    """numpy scalars are not JSON-serialisable; unwrap them."""
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (ValueError, AttributeError):
            pass
    return value


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #


class JobManager:
    """Owns the job table and the worker pool."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._pool: ThreadPoolExecutor | None = None

    def _executor(self) -> ThreadPoolExecutor:
        """The worker pool, created on demand and re-created after shutdown.

        A pool that cannot be restarted turns any in-process restart of the app
        into a manager that silently accepts uploads and never runs them. Since
        the manager is a module-level singleton and shutdown fires on the
        application's lifespan, that is one reload away from happening for real.
        """
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(
                    max_workers=MAX_WORKERS, thread_name_prefix="pdf2csv"
                )
            return self._pool

    # -- lifecycle -----------------------------------------------------------
    def submit(self, filename: str, data: bytes, *, profile: str | None = None) -> Job:
        settings = get_settings()
        settings.ensure_dirs()

        job_id = uuid.uuid4().hex[:12]
        safe_name = _safe_filename(filename)
        upload_dir = settings.work_dir / job_id
        upload_dir.mkdir(parents=True, exist_ok=True)

        upload_path = upload_dir / safe_name
        upload_path.write_bytes(data)

        output_dir = settings.output_dir / f"{Path(safe_name).stem}-{job_id}"
        output_dir.mkdir(parents=True, exist_ok=True)

        job = Job(
            id=job_id,
            filename=safe_name,
            size_bytes=len(data),
            upload_path=upload_path,
            output_dir=output_dir,
        )
        job.record("reading", 0, 1, "Uploaded — waiting to start")

        with self._lock:
            self._jobs[job_id] = job
            self._order.append(job_id)

        self._executor().submit(self._process, job, profile)
        self._sweep()
        log.info("job %s queued: %s (%.1f KB)", job_id, safe_name, len(data) / 1024)
        return job

    def _process(self, job: Job, profile: str | None) -> None:
        from pdf2csv.core.pipeline import run

        job.status = JobStatus.RUNNING
        job.started_at = time.time()
        try:
            # A certificat de dépôt declaration is not a table to transcribe —
            # it is five facts that derive a fixed row. Reading it with the
            # ordinary table extractor produces something that looks like data
            # and is not, so it is tried first and only for documents that
            # actually look like one.
            result = self._try_declaration(job)
            if result is None:
                result = run(job.upload_path, profile=profile, progress=job.record)
            job.result = result

            stem = Path(job.filename).stem
            # The finance team's reference files are semicolon-delimited,
            # which these rows need because their values contain commas.
            delimiter = ";" if result.meta.profile == "declaration" else ","
            job.exports = export_result(
                result, job.output_dir / f"{stem}.csv", delimiter=delimiter
            )

            job.status = JobStatus.DONE
            job.record("done", 1, 1, result.report.summary())
            log.info("job %s finished in %.1fs", job.id, job.duration)
        except Exception as exc:
            job.status = JobStatus.FAILED
            job.error = str(exc) or exc.__class__.__name__
            job.record("done", 1, 1, "Could not read this file")
            log.exception("job %s failed", job.id)
        finally:
            job.finished_at = time.time()

    def _try_declaration(self, job: Job):
        """Read the upload as a declaration, or return ``None``.

        Never allowed to fail the job: if this path raises for any reason, the
        ordinary table extractor still runs and the analyst still gets output.
        """
        try:
            from pdf2csv.declarations.pipeline import looks_like_declaration, run_declaration

            if not looks_like_declaration(job.upload_path):
                return None
            return run_declaration(job.upload_path, progress=job.record)
        except Exception:
            log.exception("job %s: declaration path failed, falling back", job.id)
            return None

    # -- access --------------------------------------------------------------
    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 20) -> list[Job]:
        with self._lock:
            ids = list(reversed(self._order))[:limit]
            return [self._jobs[i] for i in ids if i in self._jobs]

    def remove(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
            if job_id in self._order:
                self._order.remove(job_id)
        if job is None:
            return False
        job.cleanup()
        return True

    def _sweep(self) -> None:
        """Drop the oldest finished jobs once the table grows past the limit.

        Client PDFs and their extracts are sitting on disk here. Keeping them
        forever is a slow-motion data-retention problem on someone else's
        machine, so old ones are removed rather than accumulated.
        """
        keep = get_settings().retain_jobs
        with self._lock:
            excess = [
                job_id
                for job_id in self._order[: max(0, len(self._order) - keep)]
                if (job := self._jobs.get(job_id)) and job.is_finished
            ]
        for job_id in excess:
            self.remove(job_id)

    def shutdown(self) -> None:
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_UNSAFE = '<>:"/\\|?*'


def _safe_filename(name: str) -> str:
    """Reduce an uploaded name to something safe to join onto a path.

    The browser sends whatever the file was called. ``..\\..\\Windows\\x.pdf``
    is a legal upload name and must not be able to escape the work directory.
    """
    base = Path(name).name  # discards any directory component
    cleaned = "".join("_" if ch in _UNSAFE or ord(ch) < 32 else ch for ch in base).strip()
    cleaned = cleaned.strip(". ") or "document.pdf"
    if not cleaned.lower().endswith(".pdf"):
        cleaned += ".pdf"
    return cleaned[:120]


def reveal_in_file_manager(path: Path) -> bool:
    """Open the OS file manager at ``path``.

    Small feature, disproportionate value: the analyst's next step after a
    successful run is to find the CSV, and "it is in C:\\Users\\...\\output"
    is a worse answer than a window that is already open at it. Only reachable
    from a loopback-bound UI on the user's own desktop.
    """
    try:
        if not path.exists():
            return False
        if sys.platform == "win32":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except OSError as exc:
        log.warning("could not open %s: %s", path, exc)
        return False
    return True
