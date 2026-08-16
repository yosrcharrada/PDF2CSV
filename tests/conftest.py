"""Shared test fixtures.

The important one is :func:`isolate_home`. Settings are resolved once and
memoised, and they decide where logs, caches and output go — so without
isolation the suite would write into the developer's real working directories
and, worse, share an OCR cache between tests. Every test gets its own home.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

FIXTURE_DIR = Path(__file__).parent / "fixtures"
PDF_DIR = FIXTURE_DIR / "pdfs"
EXPECTED_DIR = FIXTURE_DIR / "expected"


@pytest.fixture(autouse=True)
def isolate_home(tmp_path, monkeypatch):
    """Point every path-producing setting at a throwaway directory."""
    from pdf2csv.config import reset_settings_cache

    monkeypatch.setenv("PDF2CSV_HOME", str(tmp_path))
    monkeypatch.setenv("PDF2CSV_OUTPUT", str(tmp_path / "output"))
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def pdf_dir() -> Path:
    if not PDF_DIR.is_dir() or not any(PDF_DIR.glob("*.pdf")):
        pytest.skip("fixture PDFs missing — run tests/fixtures/make_fixtures.py")
    return PDF_DIR


@pytest.fixture
def ruled_statement(pdf_dir: Path) -> Path:
    return pdf_dir / "statement_ruled_2page.pdf"


@pytest.fixture
def borderless_statement(pdf_dir: Path) -> Path:
    return pdf_dir / "statement_borderless_fr.pdf"


@pytest.fixture
def broken_statement(pdf_dir: Path) -> Path:
    return pdf_dir / "statement_broken.pdf"


@pytest.fixture
def letter(pdf_dir: Path) -> Path:
    return pdf_dir / "letter_no_table.pdf"


def pytest_configure(config):
    """Skip OCR-marked tests cleanly when the add-on is not installed."""
    config.addinivalue_line("markers", "ocr: requires the OCR extra")


@pytest.fixture(scope="session")
def ocr_available() -> bool:
    from pdf2csv.core import ocr

    return ocr.is_available()
