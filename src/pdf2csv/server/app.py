"""The local HTTP API and static host.

Bound to loopback only. There is no authentication and none is wanted: the
security boundary is the operating system's, exactly as it is for Excel. What
matters instead is that nothing here can reach the network, because the
documents passing through are client financials.

Three things in this file exist specifically to guarantee that:

* ``docs_url=None`` — FastAPI's interactive docs load Swagger UI from a CDN.
  Left enabled they break on an air-gapped desktop and phone out on a
  connected one, which is precisely the failure the offline design is meant to
  prevent.
* A strict ``Content-Security-Policy`` on every response, so that even if a
  future edit slips in a remote font or script, the browser refuses it rather
  than the machine quietly making a request.
* No analytics, no telemetry, no version check.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from pdf2csv import __version__
from pdf2csv.config import get_settings
from pdf2csv.core import cache, ocr
from pdf2csv.logging_setup import get_logger, setup_logging
from pdf2csv.server.jobs import JobManager, JobStatus, reveal_in_file_manager

log = get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Everything must come from this origin. 'unsafe-inline' covers the app's own
# inline styles; no remote origin is permitted anywhere.
CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)

manager = JobManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    manager.shutdown()


def create_app() -> FastAPI:
    setup_logging()

    app = FastAPI(
        title="PDF2CSV",
        version=__version__,
        docs_url=None,  # Swagger UI is CDN-hosted; see the module docstring
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    _register_api(app)

    if STATIC_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        page = STATIC_DIR / "index.html"
        if not page.is_file():
            raise HTTPException(500, "The interface files are missing from this install.")
        return HTMLResponse(page.read_text(encoding="utf-8"))

    return app


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


def _register_api(app: FastAPI) -> None:
    settings = get_settings()

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        """Status the UI shows on first paint, including whether OCR is present."""
        return {
            "ok": True,
            "version": __version__,
            "ocr": {
                "available": ocr.is_available(),
                "reason": ocr.unavailable_reason(),
            },
            "limits": {
                "max_upload_mb": settings.max_upload_mb,
                "max_pages": settings.max_pages,
            },
            "paths": {
                "output": str(settings.output_dir),
                "logs": str(settings.logs_dir),
            },
            "cache_bytes": cache.size_bytes(),
        }

    @app.post("/api/jobs", status_code=202)
    async def create_job(
        file: UploadFile,
        profile: str | None = Query(default=None),
    ) -> dict[str, Any]:
        filename = file.filename or "document.pdf"
        if not filename.lower().endswith(".pdf"):
            raise HTTPException(415, "That is not a PDF. Please choose a .pdf file.")

        data = await _read_limited(file, settings.max_upload_bytes)

        # Check the magic bytes rather than trusting the extension: a renamed
        # .docx produces a far more confusing failure deeper in the pipeline.
        if not data.startswith(b"%PDF"):
            raise HTTPException(
                415,
                "That file is named .pdf but is not a PDF. "
                "It may have been renamed, or the download may be incomplete.",
            )

        job = manager.submit(filename, data, profile=profile)
        return job.summary()

    @app.get("/api/jobs")
    async def list_jobs(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        return {"jobs": [job.summary() for job in manager.recent(limit)]}

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        return _require(job_id).summary()

    @app.get("/api/jobs/{job_id}/preview")
    async def preview(
        job_id: str,
        limit: int = Query(default=500, ge=1, le=5000),
        offset: int = Query(default=0, ge=0),
        table: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        job = _require(job_id)
        if job.status is not JobStatus.DONE:
            raise HTTPException(409, "This document has not finished processing yet.")
        if table >= max(1, job.table_count()):
            raise HTTPException(404, "There is no table with that number in this document.")
        return job.preview(limit=limit, offset=offset, table=table)

    @app.get("/api/jobs/{job_id}/tables")
    async def tables(job_id: str) -> dict[str, Any]:
        """Every table found, each with its own reconciliation result.

        A document is not always one table, and returning only the largest
        silently discards the rest.
        """
        job = _require(job_id)
        if job.result is None:
            raise HTTPException(409, "This document has not finished processing yet.")
        return {"tables": [t.to_dict() for t in job.result.tables_out]}

    @app.get("/api/jobs/{job_id}/events")
    async def events(job_id: str, request: Request) -> StreamingResponse:
        """Server-sent progress.

        SSE rather than a WebSocket: the traffic is one-directional, it
        reconnects by itself, and it is plain HTTP — which matters when the
        client's endpoint protection is inspecting what this process does.
        """
        job = _require(job_id)

        async def stream():
            sent = 0
            while True:
                if await request.is_disconnected():
                    return

                for event in job.events_since(sent):
                    sent = event.sequence + 1
                    yield f"data: {json.dumps(event.to_dict())}\n\n"

                if job.is_finished:
                    yield f"event: complete\ndata: {json.dumps(job.summary())}\n\n"
                    return

                await asyncio.sleep(0.2)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/jobs/{job_id}/download/{kind}")
    async def download(
        job_id: str, kind: str, table: int = Query(default=0, ge=0)
    ) -> FileResponse:
        job = _require(job_id)
        if job.exports is None:
            raise HTTPException(409, "Nothing has been produced for this document yet.")

        # Secondary tables are written alongside as <name>.table2.csv and so on.
        # The workbook already holds every table as its own sheet, and the JSON
        # report covers the document, so only CSV varies by table.
        if kind == "csv" and table > 0:
            position = table - 1
            if position >= len(job.exports.extras):
                raise HTTPException(404, "There is no table with that number.")
            path = job.exports.extras[position]
            return FileResponse(path, media_type="text/csv", filename=path.name)

        targets = {
            "csv": (job.exports.csv, "text/csv"),
            "xlsx": (
                job.exports.xlsx,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
            "json": (job.exports.report_json, "application/json"),
        }
        if kind not in targets:
            raise HTTPException(404, "Unknown download type.")

        path, media_type = targets[kind]
        if path is None or not path.is_file():
            raise HTTPException(404, "That file was not produced for this document.")

        return FileResponse(path, media_type=media_type, filename=path.name)

    @app.post("/api/jobs/{job_id}/reveal")
    async def reveal(job_id: str) -> dict[str, Any]:
        job = _require(job_id)
        opened = reveal_in_file_manager(job.output_dir)
        return {"opened": opened, "path": str(job.output_dir)}

    @app.delete("/api/jobs/{job_id}")
    async def delete_job(job_id: str) -> dict[str, Any]:
        if not manager.remove(job_id):
            raise HTTPException(404, "No such document.")
        return {"deleted": True}

    @app.post("/api/cache/clear")
    async def clear_cache() -> dict[str, Any]:
        return {"reclaimed_bytes": cache.clear()}


def _require(job_id: str):
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, "No such document — it may have been cleared.")
    return job


async def _read_limited(file: UploadFile, limit: int) -> bytes:
    """Read an upload, refusing anything over the limit.

    Read in chunks and stop at the ceiling rather than calling ``.read()`` and
    checking afterwards: the latter has already put the whole file in memory by
    the time it can object, which is the wrong order of events when the point
    is to not accept a 4 GB upload.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(1 << 20):
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                413,
                f"That file is larger than the {limit // (1024 * 1024)} MB limit.",
            )
        chunks.append(chunk)

    if total == 0:
        raise HTTPException(400, "That file is empty.")
    return b"".join(chunks)


# Exception handler so the UI always receives {"detail": "..."} it can display.
def _install_error_handler(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


app = create_app()
_install_error_handler(app)
