"""Runtime settings and the one place that decides where files go.

Two deployments have to work from the same code:

* **Developer checkout** — writable repo, ``PDF2CSV_HOME`` unset. Everything
  lands in the repo root under gitignored folders.
* **Portable bundle on a client desktop** — ``Run.bat`` exports
  ``PDF2CSV_HOME`` pointing at the bundle folder. If that folder turns out to
  be read-only (it happens: bundles get dropped on a network share, or into
  ``Program Files``), we fall back to ``%LOCALAPPDATA%\\PDF2CSV`` rather than
  crashing on first write. An analyst should never see a permissions traceback.

Every value can be overridden by an environment variable, which is what makes
the batch launchers configurable without editing Python.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

APP_NAME = "PDF2CSV"


# --------------------------------------------------------------------------- #
# Environment helpers
# --------------------------------------------------------------------------- #


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- #
# Locating the home directory
# --------------------------------------------------------------------------- #


def _repo_root() -> Path | None:
    """Walk up from this file looking for the checkout root."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return None


def _is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".pdf2csv-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        return False
    return True


def _fallback_home() -> Path:
    """A per-user location that is writable on any Windows profile."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path(tempfile.gettempdir()) / APP_NAME


def resolve_home() -> Path:
    """Pick the writable directory that owns logs, work files and output."""
    candidates: list[Path] = []

    explicit = os.environ.get("PDF2CSV_HOME", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())

    repo = _repo_root()
    if repo is not None:
        candidates.append(repo)

    candidates.append(_fallback_home())

    for candidate in candidates:
        if _is_writable(candidate):
            return candidate.resolve()

    # Nothing writable anywhere. Temp always is, or the OS is broken.
    last = Path(tempfile.gettempdir()) / APP_NAME
    last.mkdir(parents=True, exist_ok=True)
    return last.resolve()


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of configuration, resolved once per process."""

    home: Path

    # --- Extraction ---------------------------------------------------------
    min_text_chars: int = 50
    """Below this many characters on a page, treat it as scanned.

    Tuned against real statements: a genuinely digital page carries hundreds of
    characters, while the stray text layer a bad scanner leaves behind (a page
    number, a watermark) carries a handful. 50 sits in the empty middle.
    """

    ocr_dpi: int = 300
    """The floor for statement fonts. Below it small digits lose strokes and
    ``8`` starts reading as ``3``. Above 400 you pay double the time for a
    fraction of a percent of accuracy."""

    low_confidence: float = 0.80
    """OCR confidence under this in a numeric cell becomes a review flag."""

    ragged_tolerance: float = 0.20
    """How much row-length variation is tolerated before the lattice result is
    judged wrong and the stream strategy is tried instead."""

    max_pages: int = 500
    """Refuse absurd documents rather than appearing to hang for an hour."""

    # --- Web UI -------------------------------------------------------------
    host: str = "127.0.0.1"
    """Loopback only. There is no reason to expose a client's financial
    documents to their office network, and doing so turns a desktop tool into
    something their security team has to review."""

    port: int = 8730
    max_upload_mb: int = 200
    open_browser: bool = True

    # --- Housekeeping -------------------------------------------------------
    log_level: str = "INFO"
    retain_jobs: int = 40
    """How many finished jobs stay on disk before the oldest is swept."""

    cache_enabled: bool = True

    # --- Derived paths ------------------------------------------------------
    _dirs: dict[str, Path] = field(default_factory=dict, repr=False, compare=False)

    # Paths ------------------------------------------------------------------
    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def work_dir(self) -> Path:
        """Uploads and per-job output. Contains client data; never committed."""
        return self.home / "work"

    @property
    def cache_dir(self) -> Path:
        return self.home / ".cache"

    @property
    def output_dir(self) -> Path:
        """Where finished CSVs are kept so they survive the browser download."""
        explicit = os.environ.get("PDF2CSV_OUTPUT", "").strip()
        return Path(explicit).expanduser() if explicit else self.home / "output"

    @property
    def models_dir(self) -> Path:
        """Pre-downloaded OCR weights. Populated by the packaging script."""
        explicit = os.environ.get("PDF2CSV_MODELS", "").strip()
        return Path(explicit).expanduser() if explicit else self.home / "models"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def ensure_dirs(self) -> None:
        for path in (self.logs_dir, self.work_dir, self.cache_dir, self.output_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Resolved once and memoised — call it freely from anywhere."""
    return Settings(
        home=resolve_home(),
        min_text_chars=_env_int("PDF2CSV_MIN_TEXT_CHARS", 50),
        ocr_dpi=_env_int("PDF2CSV_OCR_DPI", 300),
        low_confidence=_env_float("PDF2CSV_LOW_CONFIDENCE", 0.80),
        ragged_tolerance=_env_float("PDF2CSV_RAGGED_TOLERANCE", 0.20),
        max_pages=_env_int("PDF2CSV_MAX_PAGES", 500),
        host=_env_str("PDF2CSV_HOST", "127.0.0.1"),
        port=_env_int("PDF2CSV_PORT", 8730),
        max_upload_mb=_env_int("PDF2CSV_MAX_UPLOAD_MB", 200),
        open_browser=_env_bool("PDF2CSV_OPEN_BROWSER", True),
        log_level=_env_str("PDF2CSV_LOG_LEVEL", "INFO").upper(),
        retain_jobs=_env_int("PDF2CSV_RETAIN_JOBS", 40),
        cache_enabled=_env_bool("PDF2CSV_CACHE", True),
    )


def reset_settings_cache() -> None:
    """Drop the memoised settings. Tests use this after patching the env."""
    get_settings.cache_clear()
