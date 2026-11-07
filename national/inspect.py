"""Print the raw structure of any list file so a parser can be fixed.

    python -m national.inspect <url> [<url> ...]

Shows: detected format, every sheet's first 30 rows (Excel/CSV), the first
tables/lines of a PDF or web page, and what the generic extractor got.
"""
from __future__ import annotations

import io
import logging
import sys

from . import extract, http


def show(url: str) -> None:
    print("=" * 100, "\n", url)
    f = http.fetch(url, check_robots=False)
    if not f or f.status != 200:
        print("  download failed:", getattr(f, "status", "error"))
        return
    fmt = extract.detect_format(f.body, f.url, f.content_type)
    print(f"  {len(f.body):,} bytes  content-type={f.content_type!r}  format={fmt}")
    try:
        if fmt == "xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(f.body), read_only=False, data_only=True)
            for ws in wb.worksheets:
                print(f"  --- sheet {ws.title!r}  dims={ws.dimensions}  max_row={ws.max_row} max_col={ws.max_column}"
                      f"  merged={list(ws.merged_cells.ranges)[:8]}")
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i >= 30:
                        break
                    print(f"  {i:>3}: {[c for c in row]}")
        elif fmt in ("xls", "csv"):
            tabs = extract.tables_from_xls(f.body) if fmt == "xls" else extract.tables_from_csv(f.body)
            for t in tabs:
                for i, row in enumerate(t[:30]):
                    print(f"  {i:>3}: {row}")
        elif fmt == "pdf":
            tables, text = extract.pdf_tables_and_text(f.body, max_pages=3)
            for t in tables[:1]:
                for i, row in enumerate(t[:20]):
                    print(f"  T{i:>3}: {row}")
            for i, line in enumerate(text.splitlines()[:40]):
                print(f"  L{i:>3}: {line}")
        else:
            tables, text = extract.tables_from_html(f.body) if fmt == "html" else ([], f.text)
            for t in tables[:2]:
                for i, row in enumerate(t[:15]):
                    print(f"  T{i:>3}: {row}")
            for i, line in enumerate([l for l in text.splitlines() if l.strip()][:40]):
                print(f"  L{i:>3}: {line.strip()[:200]}")
    except Exception as exc:  # noqa: BLE001
        print("  raw dump error:", exc)
    ex = extract.extract(f.body, f.url, f.content_type)
    print(f"  EXTRACTOR: method={ex.method} rows={len(ex.rows)} quality={ex.quality} total={ex.total} cols={ex.columns} note={ex.note}")
    for r in ex.rows[:5]:
        print("    ", r)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    for noisy in ("pdfminer", "pdfplumber"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    for u in sys.argv[1:]:
        show(u)
