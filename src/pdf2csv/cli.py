"""Command line entry point.

Four subcommands, and ``ui`` is the default because that is what the analyst
double-clicks. The others exist for the person supporting them:

``convert``  batch or scripted use, and the fastest way to reproduce a bug
``check``    an environment report to paste into an email when it will not start
``cache``    clear the OCR cache when a document is reprocessed after a fix

Argparse rather than click or typer: this has to run inside an embeddable
Python distribution, and every dependency that is not strictly necessary is one
more wheel that can fail to install on a locked-down desktop.
"""

from __future__ import annotations

import argparse
import contextlib
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

from pdf2csv import __version__
from pdf2csv.config import get_settings
from pdf2csv.logging_setup import get_logger, setup_logging

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# ui
# --------------------------------------------------------------------------- #


def find_free_port(preferred: int, host: str, attempts: int = 25) -> int:
    """Return a bindable port, starting at ``preferred``.

    Ports get taken. If the analyst left yesterday's window open, or some other
    tool claimed 8730, the right behaviour is to move to 8731 and carry on —
    not to fail with 'address already in use', which reads as broken software
    to someone who does not know what a port is.
    """
    for offset in range(attempts):
        candidate = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, candidate))
            except OSError:
                continue
            return candidate
    return preferred


def command_ui(args: argparse.Namespace) -> int:
    import uvicorn

    settings = get_settings()
    settings.ensure_dirs()
    setup_logging()

    host = args.host or settings.host
    port = find_free_port(args.port or settings.port, host)
    url = f"http://{host}:{port}"

    if port != (args.port or settings.port):
        log.info("preferred port was busy; using %d", port)

    print()
    print("  PDF2CSV is running.")
    print()
    print(f"    Open this in your browser:  {url}")
    print(f"    Finished files are saved to: {settings.output_dir}")
    print()
    print("  Leave this window open while you work. Close it to stop.")
    print()

    if settings.open_browser and not args.no_browser:
        def _open() -> None:
            # Give uvicorn a moment to bind, or the browser races it and shows
            # a connection error the analyst then has to refresh past.
            time.sleep(1.2)
            # A desktop with no default browser configured must not take the
            # server down with it; the URL is printed above either way.
            with contextlib.suppress(Exception):
                webbrowser.open(url)

        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run(
        "pdf2csv.server.app:app",
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    return 0


# --------------------------------------------------------------------------- #
# convert
# --------------------------------------------------------------------------- #


def command_convert(args: argparse.Namespace) -> int:
    from pdf2csv.core.export import export_result
    from pdf2csv.core.pipeline import run

    setup_logging(level="DEBUG" if args.verbose else None)

    source = Path(args.pdf)
    destination = Path(args.output) if args.output else source.with_suffix(".csv")

    def show(stage: str, current: int, total: int, message: str) -> None:
        if not args.quiet:
            print(f"  {message}", flush=True)

    try:
        result = run(
            source,
            profile=args.profile,
            progress=show,
            enable_ocr=not args.no_ocr,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"\n  {exc}\n", file=sys.stderr)
        return 2

    export_result(result, destination, write_xlsx=not args.no_xlsx)

    # Non-zero when the numbers did not reconcile, so a scripted caller can
    # act on it. The files are still written either way.
    return 0 if result.report.passed else 1


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #


def command_check(args: argparse.Namespace) -> int:
    """Print an environment report.

    Written to be pasted into an email. When a client desktop will not run
    this, the answer is almost always in these twenty lines, and asking a
    non-technical user to investigate any other way does not work.
    """
    import platform

    from pdf2csv.core import cache, ocr

    settings = get_settings()

    print()
    print(f"  PDF2CSV {__version__}")
    print("  " + "-" * 60)
    print(f"  Python           {platform.python_version()}  ({sys.executable})")
    print(f"  Platform         {platform.platform()}")
    print()

    print("  Dependencies")
    for module in (
        "pdfplumber", "pypdfium2", "pandas", "numpy", "fastapi",
        "uvicorn", "openpyxl", "cv2", "onnxruntime", "rapidocr_onnxruntime",
    ):
        try:
            imported = __import__(module)
            version = getattr(imported, "__version__", "installed")
            print(f"    {module:24s} {version}")
        except ImportError:
            print(f"    {module:24s} MISSING")
    print()

    print("  OCR (scanned pages)")
    report = ocr.model_report()
    if report["available"]:
        print("    available        yes")
        for model in report["models"]:
            print(f"    model            {model['name']}  ({model['size_mb']} MB)")
        # ASCII only: a Windows console on a client machine may be running any
        # codepage, and a mojibake diagnostic is a diagnostic nobody trusts.
        print("    downloads needed no - the weights ship inside the package")
    else:
        print("    available        NO")
        print(f"    reason           {report['reason']}")
    print()

    _report_isolation()

    print("  Folders")
    for label, path in (
        ("home", settings.home),
        ("output", settings.output_dir),
        ("logs", settings.logs_dir),
        ("cache", settings.cache_dir),
    ):
        writable = "writable" if _writable(path) else "NOT WRITABLE"
        print(f"    {label:16s} {path}  [{writable}]")
    print(f"    cache size       {cache.size_bytes() / 1e6:.1f} MB")
    print()

    problems = [
        name for name in ("pdfplumber", "pypdfium2", "pandas", "fastapi") if not _importable(name)
    ]
    if problems:
        print(f"  PROBLEM: missing required packages: {', '.join(problems)}")
        return 1
    if not _writable(settings.output_dir):
        print("  PROBLEM: the output folder cannot be written to.")
        return 1

    print("  Everything needed to run is present.")
    print()
    return 0


def _report_isolation() -> None:
    """Report any import path that comes from outside this installation.

    Worth its own section because it is the failure that reproduces on exactly
    one desktop and nowhere else. The bundled runtime must enable ``import
    site``, which also switches on the per-user site-packages folder; if the
    machine has any Python 3.11 packages there, they can shadow the versions
    that shipped in the bundle.
    """
    import site
    import sysconfig

    print("  Python isolation")

    user_site_on = getattr(site, "ENABLE_USER_SITE", None)
    print(f"    user site-packages {'ENABLED' if user_site_on else 'disabled'}")

    import pdf2csv

    # "Inside" means the interpreter's own tree, the standard library, or the
    # installation the pdf2csv package was imported from. In the portable bundle
    # those are two siblings — PDF2CSV\python and PDF2CSV\app — so comparing
    # against the interpreter's folder alone reports the application's own code
    # as foreign, which is exactly the false alarm that makes a diagnostic
    # useless.
    #
    # The grandparent is included for the same reason: `python -m pdf2csv` puts
    # the working directory on sys.path, which in a development checkout is the
    # repository root above src/, and in the bundle is the bundle root above
    # app/. Both are the installation. Without this the check warns on every
    # ordinary development run, and a warning that fires when nothing is wrong
    # is one nobody reads when something is.
    package_dir = Path(pdf2csv.__file__).resolve().parent
    roots: list[Path] = []
    for candidate in (
        sys.prefix,
        sys.base_prefix,
        sysconfig.get_paths()["stdlib"],
        str(package_dir.parent),
        str(package_dir.parent.parent),
    ):
        try:
            roots.append(Path(candidate).resolve())
        except OSError:
            continue

    foreign = []
    for entry in sys.path:
        if not entry:
            continue
        try:
            resolved = Path(entry).resolve()
        except OSError:
            continue
        if any(root == resolved or root in resolved.parents for root in roots):
            continue
        foreign.append(str(resolved))

    if foreign:
        print("    WARNING: these import paths are outside this installation")
        for entry in foreign:
            print(f"      {entry}")
        print("    Packages found there can override the ones that shipped here.")
        print("    Start the tool with 'Start PDF2CSV.bat', which prevents this.")
    else:
        print("    import paths     all inside this installation")
    print()


def _importable(name: str) -> bool:
    try:
        __import__(name)
    except ImportError:
        return False
    return True


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #


def command_cache(args: argparse.Namespace) -> int:
    from pdf2csv.core import cache

    if args.cache_action == "clear":
        print(f"  Cleared {cache.clear() / 1e6:.1f} MB of cached OCR results.")
    else:
        print(f"  OCR cache: {cache.size_bytes() / 1e6:.1f} MB")
    return 0


# --------------------------------------------------------------------------- #
# declare
# --------------------------------------------------------------------------- #


def command_declare(args: argparse.Namespace) -> int:
    """Turn a certificat de dépôt declaration into a standard row.

    A different job from ``convert``: that reads whatever table is in the PDF,
    while this reads five known facts and derives a fixed row from them. Kept as
    its own command because the two have nothing in common but the file type.
    """
    import csv

    from pdf2csv.declarations.facts import extract_declarations
    from pdf2csv.declarations.mapping import COLUMNS, reconcile, to_row

    setup_logging(level="DEBUG" if args.verbose else "WARNING")

    source = Path(args.pdf)
    if not source.is_file():
        print(f"\n  No such file: {source}\n", file=sys.stderr)
        return 2

    def show(current: int, total: int, message: str) -> None:
        if not args.quiet:
            print(f"  {message}", flush=True)

    try:
        facts_list = extract_declarations(str(source), dpi=args.dpi, progress=show)
    except RuntimeError as exc:  # OCR add-on missing
        print(f"\n  {exc}\n", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"\n  {source.name} could not be read: {exc}\n", file=sys.stderr)
        return 2

    if not facts_list:
        print(
            f"\n  No declaration could be read from {source.name}.\n"
            "  This path handles single-declaration documents. Multi-row\n"
            "  'Billet de Tresorerie' fiches are not supported yet.\n",
            file=sys.stderr,
        )
        return 2

    pool = ledger = None
    if args.isin_pool:
        from pdf2csv.declarations.isin import AllocationError, IsinLedger, IsinPool, allocate

        try:
            pool = IsinPool.load(args.isin_pool)
            ledger_path = (
                Path(args.ledger)
                if args.ledger
                else get_settings().home / "isin_ledger.json"
            )
            ledger = IsinLedger.load(ledger_path)
        except AllocationError as exc:
            print(f"\n  {exc}\n", file=sys.stderr)
            return 2

    rows: list[dict] = []
    failed = 0

    for facts in facts_list:
        isin, reused = "", False
        if pool is not None and ledger is not None and not args.dry_run:
            from pdf2csv.declarations.isin import AllocationError, allocate

            try:
                isin, reused = allocate(facts, pool, ledger)
            except AllocationError as exc:
                print(f"\n  {exc}\n", file=sys.stderr)
                return 2

        try:
            row = to_row(facts, isin=isin)
        except ValueError as exc:
            print(f"\n  {exc}\n", file=sys.stderr)
            return 2
        rows.append(row)

        print()
        print(f"  Page {facts.source_page}  (read with {facts.confidence:.0%} confidence)")
        print("  " + "-" * 62)
        print(f"    title              {facts.title}")
        print(f"    taux               {facts.taux}")
        print(f"    quantite           {facts.quantite}")
        print(f"    souscription       {facts.date_souscription}")
        print(f"    remboursement      {facts.date_remboursement}")
        if facts.prix_unitaire is not None:
            print(f"    prix unitaire      {facts.prix_unitaire:,.3f}")
        if facts.montant is not None:
            print(f"    montant            {facts.montant:,.3f}")
        print()
        for name in COLUMNS:
            print(f"    {name:28s} {row[name]}")

        print()
        checks = reconcile(facts)
        if pool is not None and not args.dry_run:
            from pdf2csv.declarations.isin import allocation_check

            checks.append(allocation_check(isin, reused))
        for check in checks:
            mark = "ok  " if check["passed"] else "FAIL"
            if not check["passed"]:
                failed += 1
            print(f"    [{mark}] {check['title']}: {check['detail']}")

    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Semicolon-delimited and UTF-8 with a BOM, matching the reference
        # files from the finance team and opening correctly in Windows Excel.
        with destination.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS, delimiter=";")
            writer.writeheader()
            writer.writerows(rows)
        print(f"\n  Written to {destination}")

    if pool is None:
        print(
            "\n  Note: no --isin-pool was given, so the ISIN column is empty.\n"
            "  Pass --isin-pool \"block d ISIN.xlsx\" to allocate one."
        )

    print(
        # ASCII only. A Windows console may be running any codepage, and a
        # mojibake note is a note nobody reads.
        "\n  Note: this writes the 22 confirmed fields; the finance team's\n"
        "  reference files use 36. Four rules are still unresolved:\n"
        "    auctionDate, code, amountToBePaid, and nominal -\n"
        "    your answer said 500 x quantity, but all four reference rows\n"
        "    show 500 000 x quantity (i.e. the montant).\n"
        "  See docs/DECLARATIONS.md.\n"
    )
    return 0 if failed == 0 else 1


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf2csv",
        description="Turn finance PDFs into validated CSV files.",
    )
    parser.add_argument("--version", action="version", version=f"pdf2csv {__version__}")
    subcommands = parser.add_subparsers(dest="command")

    ui = subcommands.add_parser("ui", help="start the web interface (default)")
    ui.add_argument("--host", default=None)
    ui.add_argument("--port", type=int, default=None)
    ui.add_argument("--no-browser", action="store_true", help="do not open a browser")
    ui.set_defaults(func=command_ui)

    convert = subcommands.add_parser("convert", help="convert one PDF from the command line")
    convert.add_argument("pdf")
    convert.add_argument("-o", "--output", help="destination CSV (default: alongside the PDF)")
    convert.add_argument("-p", "--profile", help="document profile name")
    convert.add_argument("--no-ocr", action="store_true", help="skip scanned pages")
    convert.add_argument("--no-xlsx", action="store_true", help="do not write the workbook")
    convert.add_argument("-q", "--quiet", action="store_true")
    convert.add_argument("-v", "--verbose", action="store_true")
    convert.set_defaults(func=command_convert)

    declare = subcommands.add_parser(
        "declare",
        help="read a certificat de depot declaration into a standard row",
    )
    declare.add_argument("pdf")
    declare.add_argument("-o", "--output", help="destination CSV")
    declare.add_argument(
        "--isin-pool", help="the 'block d ISIN' workbook; omit to leave ISIN empty"
    )
    declare.add_argument("--ledger", help="allocation ledger (default: alongside the logs)")
    declare.add_argument(
        "--dry-run",
        action="store_true",
        help="do not consume an ISIN, just show what would be produced",
    )
    declare.add_argument("--dpi", type=int, default=200)
    declare.add_argument("-q", "--quiet", action="store_true")
    declare.add_argument("-v", "--verbose", action="store_true")
    declare.set_defaults(func=command_declare)

    check = subcommands.add_parser("check", help="print an environment report")
    check.set_defaults(func=command_check)

    cache_command = subcommands.add_parser("cache", help="inspect or clear the OCR cache")
    cache_command.add_argument(
        "cache_action", nargs="?", choices=["status", "clear"], default="status"
    )
    cache_command.set_defaults(func=command_cache)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "func", None):
        # Bare `pdf2csv` starts the UI. The launcher batch file relies on this,
        # and it is the only thing a non-technical user will ever run.
        args = parser.parse_args([*(argv or []), "ui"])

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n  Stopped.\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
