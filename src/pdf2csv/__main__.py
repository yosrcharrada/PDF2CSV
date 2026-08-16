"""Allows ``python -m pdf2csv``, which is what the Windows launchers call."""

from pdf2csv.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
