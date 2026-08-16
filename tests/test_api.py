"""Tests for the HTTP API.

Two groups. The first is the happy path — upload, poll, preview, download —
because that is the only path the analyst ever takes and it must not break.

The second is the rejection path, and it matters more than it looks. Every
message the API refuses with is read by someone who cannot debug it, so each
one is asserted to be a sentence rather than a status code. "That file is named
.pdf but is not a PDF" ends the problem; "415 Unsupported Media Type" starts a
support call.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from pdf2csv.server.app import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def run_to_completion(client, path, *, filename=None, timeout=120.0):
    """Upload a fixture and poll until the job finishes."""
    name = filename or path.name
    with path.open("rb") as handle:
        response = client.post(
            "/api/jobs", files={"file": (name, handle, "application/pdf")}
        )
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]

    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.1)
    pytest.fail("job did not finish within the timeout")


class TestHealth:
    def test_reports_version_and_limits(self, client):
        body = client.get("/api/health").json()
        assert body["ok"] is True
        assert body["version"]
        assert body["limits"]["max_upload_mb"] > 0
        assert "output" in body["paths"]

    def test_reports_whether_ocr_is_installed(self, client):
        ocr = client.get("/api/health").json()["ocr"]
        assert isinstance(ocr["available"], bool)
        # When unavailable there must be a sentence explaining it, because the
        # UI shows this text verbatim in a banner.
        if not ocr["available"]:
            assert ocr["reason"]


class TestUpload:
    def test_happy_path(self, client, ruled_statement):
        job = run_to_completion(client, ruled_statement)

        assert job["status"] == "done"
        assert job["result"]["n_rows"] == 10
        assert job["result"]["passed"] is True
        assert job["percent"] == 1.0

    def test_progress_events_are_recorded(self, client, ruled_statement):
        job = run_to_completion(client, ruled_statement)
        assert job["message"]
        assert job["duration"] >= 0

    def test_rejects_a_non_pdf_extension(self, client, tmp_path):
        path = tmp_path / "notes.txt"
        path.write_text("hello", encoding="utf-8")
        with path.open("rb") as handle:
            response = client.post("/api/jobs", files={"file": ("notes.txt", handle)})

        assert response.status_code == 415
        assert "PDF" in response.json()["detail"]

    def test_rejects_a_renamed_file(self, client, tmp_path):
        """The extension is a claim; the magic bytes are the evidence.

        A .docx renamed to .pdf otherwise fails much deeper in the pipeline
        with a message about an xref table.
        """
        path = tmp_path / "statement.pdf"
        path.write_bytes(b"PK\x03\x04 this is really a zip")
        with path.open("rb") as handle:
            response = client.post(
                "/api/jobs", files={"file": ("statement.pdf", handle, "application/pdf")}
            )

        assert response.status_code == 415
        assert "not a PDF" in response.json()["detail"]

    def test_rejects_an_empty_file(self, client, tmp_path):
        path = tmp_path / "empty.pdf"
        path.write_bytes(b"")
        with path.open("rb") as handle:
            response = client.post("/api/jobs", files={"file": ("empty.pdf", handle)})

        assert response.status_code == 400

    def test_directory_traversal_in_the_filename_is_neutralised(
        self, client, ruled_statement
    ):
        """The browser sends whatever the file was called, including this."""
        job = run_to_completion(
            client, ruled_statement, filename="..\\..\\Windows\\System32\\evil.pdf"
        )
        assert ".." not in job["filename"]
        assert "\\" not in job["filename"]
        assert job["filename"].endswith(".pdf")


class TestResults:
    def test_preview_returns_typed_rows(self, client, ruled_statement):
        job = run_to_completion(client, ruled_statement)
        body = client.get(f"/api/jobs/{job['id']}/preview").json()

        assert body["total"] == 10
        assert len(body["rows"]) == 10
        assert "Balance" in body["columns"]

        balance = body["columns"].index("Balance")
        assert body["kinds"][balance] == "number"
        assert body["rows"][-1][balance] == pytest.approx(16610.90)

    def test_blank_cells_are_null_not_nan(self, client, ruled_statement):
        """NaN is not valid JSON; a browser parsing it throws and the page dies."""
        job = run_to_completion(client, ruled_statement)
        raw = client.get(f"/api/jobs/{job['id']}/preview").text
        assert "NaN" not in raw
        assert "Infinity" not in raw

    @pytest.mark.parametrize("kind", ["csv", "xlsx", "json"])
    def test_downloads(self, client, ruled_statement, kind):
        job = run_to_completion(client, ruled_statement)
        response = client.get(f"/api/jobs/{job['id']}/download/{kind}")

        assert response.status_code == 200
        assert len(response.content) > 0

    def test_csv_download_carries_the_excel_bom(self, client, borderless_statement):
        job = run_to_completion(client, borderless_statement)
        response = client.get(f"/api/jobs/{job['id']}/download/csv")
        assert response.content.startswith(b"\xef\xbb\xbf")

    def test_unknown_download_type(self, client, ruled_statement):
        job = run_to_completion(client, ruled_statement)
        assert client.get(f"/api/jobs/{job['id']}/download/exe").status_code == 404

    def test_flags_point_at_real_rows(self, client, broken_statement):
        job = run_to_completion(client, broken_statement)
        body = client.get(f"/api/jobs/{job['id']}/preview").json()

        assert job["result"]["passed"] is False
        for flag in job["result"]["flags"]:
            assert 0 <= flag["row"] < body["total"], "a flag must address an existing row"
            assert flag["column"] in body["columns"]


class TestJobLifecycle:
    def test_unknown_job(self, client):
        response = client.get("/api/jobs/deadbeef")
        assert response.status_code == 404
        assert "detail" in response.json()

    def test_listing(self, client, ruled_statement):
        run_to_completion(client, ruled_statement)
        jobs = client.get("/api/jobs").json()["jobs"]
        assert len(jobs) >= 1

    def test_delete_removes_it(self, client, ruled_statement):
        job = run_to_completion(client, ruled_statement)
        assert client.delete(f"/api/jobs/{job['id']}").status_code == 200
        assert client.get(f"/api/jobs/{job['id']}").status_code == 404

    def test_the_worker_pool_survives_a_restart(self, client, ruled_statement):
        """Shutting the app down and starting it again must not kill the pool.

        The manager is a module-level singleton and its shutdown fires on the
        application lifespan, so a pool that cannot be re-created leaves the
        second run silently accepting uploads and never processing them.
        """
        from pdf2csv.server.app import manager

        manager.shutdown()
        job = run_to_completion(client, ruled_statement)
        assert job["status"] == "done"


class TestOfflineGuarantees:
    def test_no_cdn_references_anywhere_in_the_interface(self, client):
        """One stray CDN link breaks the whole air-gapped delivery.

        It also silently sends a request from a machine handling client
        financials, which is worse than the outage.
        """
        page = client.get("/").text
        for asset in ("/assets/app.css", "/assets/app.js"):
            page += client.get(asset).text

        for marker in ("http://", "https://", "//cdn", "googleapis", "jsdelivr", "unpkg"):
            assert marker not in page.replace(
                "http://www.w3.org/2000/svg", ""
            ), f"found {marker!r} in the shipped interface"

    def test_security_headers_are_set(self, client):
        headers = client.get("/").headers
        assert "default-src 'self'" in headers["content-security-policy"]
        assert headers["x-content-type-options"] == "nosniff"

    def test_interactive_docs_are_disabled(self, client):
        """FastAPI's /docs loads Swagger UI from a CDN."""
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
