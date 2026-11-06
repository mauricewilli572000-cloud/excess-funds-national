"""Generic excess-funds list extraction for any county's file.

Every county formats its list differently, so this works in two passes:

  1. TABLE MODE — pull tables out of the file (Excel, CSV, HTML <table>,
     PDF tables via pdfplumber), find the header row, and map columns by
     name (case number, owner/claimant, amount, parcel, address, dates).
  2. LINE MODE — if no usable table, read the text line by line and pick
     out a money amount, a case or parcel number, a date, and treat the
     remaining words as the name. Rows from line mode are flagged
     "parsed from text" so you know to spot-check them.

extract(body, url) -> Extraction(rows=[...], method=..., quality=...)
"""
from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Column aliases (normalized: lowercase, non-alphanumerics -> space)
# --------------------------------------------------------------------------
ALIASES: dict[str, tuple[str, ...]] = {
    "case_number": ("case number", "case no", "case num", "case", "case id", "case nbr", "docket",
                    "docket number", "file number", "file no", "cause number", "index number",
                    "tax deed number", "deed number", "td number", "sale number", "certificate number",
                    "cert number", "certificate", "td", "matter number", "civil action number"),
    "name": ("former owner", "owner of record", "owner name", "owner", "property owner", "claimant",
             "defendant", "defendants", "debtor", "debtor name", "name", "party name", "party",
             "payee", "titleholder", "title holder", "taxpayer", "name of owner", "owners",
             "reputed owner", "last known owner", "record owner", "mortgagor"),
    "caption": ("case caption", "caption", "case name", "case title", "case style", "style",
                "description", "case description"),
    "plaintiff": ("plaintiff", "plaintiff name", "plaintiffs", "petitioner"),
    "amount": ("excess amount", "surplus amount", "excess funds", "surplus funds", "excess proceeds",
               "surplus", "excess", "overbid", "overage", "amount available", "balance",
               "amount on deposit", "deposit amount", "current balance", "amount held",
               "funds available", "amount due", "amount", "total", "funds", "net excess", "remaining"),
    "date": ("sale date", "date of sale", "auction date", "deposit date", "date deposited",
             "date received", "receipt date", "received date", "date", "posted date", "posting date",
             "redemption date", "deed date"),
    "parcel": ("parcel number", "parcel id", "parcel no", "parcel", "ppn", "pin", "apn", "account",
               "account number", "tax id", "property id", "map number", "folio", "tax map",
               "strap", "parcel control number", "pid", "geo id"),
    "address": ("property address", "situs address", "site address", "property location",
                "address", "location", "property"),
    "mailing": ("mailing address", "owner address", "last known address", "mail address"),
}
TOTAL_RE = re.compile(r"^\s*(grand\s+)?totals?\b|\bsub\s*total\b|\bpage\s+\d+\s+of\b|balance forward", re.I)
MONEY_RE = re.compile(r"(?<![\d.])\(?\$\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?\)?|(?<![\d.$])\d{1,3}(?:,\d{3})+\.\d{2}(?!\d)|(?<![\d.$])\d+\.\d{2}(?!\d)")
CASE_RE = re.compile(
    r"\b(?:(?:19|20)?\d{2}[-\s]?(?:CV|CF|CA|CE|TX|TD|TC|CH|FC|CI|SC|TF|DR|M|E|C|CVE|TAX|TL|TS)[-\s]?\d{2,8}"
    r"|[A-Z]{1,3}\d{2}-\d{3,7}|(?:19|20)\d{2}-\d{2,8}(?:-[A-Z0-9]+)?|\d{2}-[A-Z]{1,4}-\d{2,8})\b",
    re.I,
)
PARCEL_RE = re.compile(r"\b\d{2,4}[-.]\d{2,6}[-.]\d{2,6}(?:[-.]\d{1,6})*\b|\b\d{10,17}\b")
DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/(?:\d{4}|\d{2})\b|\b(?:19|20)\d{2}-\d{2}-\d{2}\b")
ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?[A-Z0-9][A-Za-z0-9.' ]{1,40}?\s(?:ST|STREET|AVE|AVENUE|RD|ROAD|DR|DRIVE|LN|LANE|"
    r"BLVD|CT|COURT|CIR|CIRCLE|WAY|PL|PLACE|TER|TERRACE|PKWY|HWY|TRL|TRAIL)\b\.?", re.I)


def norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def clean(s: Any) -> str:
    if isinstance(s, float) and s.is_integer():
        s = int(s)
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip()


def parse_money(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    s = str(v).strip()
    neg = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[^\d.\-]", "", s)
    if not s or s in ("-", "."):
        return None
    try:
        val = float(s)
    except ValueError:
        return None
    return round(-val if neg else val, 2)


def parse_date(v: Any) -> str:
    if v in (None, ""):
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (int, float)) and 20000 < float(v) < 80000:
        return (date(1899, 12, 30) + timedelta(days=int(v))).isoformat()
    s = clean(v)
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%b %d, %Y", "%B %d, %Y",
                "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M", "%d-%b-%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Row:
    case_number: str = ""
    name: str = ""
    plaintiff: str = ""
    amount: Optional[float] = None
    date: str = ""
    parcel: str = ""
    address: str = ""
    mailing: str = ""
    caption: str = ""
    raw: str = ""
    method: str = ""


@dataclass
class Extraction:
    rows: list[Row] = field(default_factory=list)
    method: str = ""
    format: str = ""
    columns: dict = field(default_factory=dict)
    note: str = ""

    @property
    def quality(self) -> float:
        if not self.rows:
            return 0.0
        good = sum(1 for r in self.rows if r.amount is not None and r.amount > 0 and (r.name or r.case_number))
        return round(good / len(self.rows), 2)

    @property
    def total(self) -> float:
        return round(sum(r.amount or 0 for r in self.rows), 2)


# --------------------------------------------------------------------------
# Format detection + table/text readers
# --------------------------------------------------------------------------

def detect_format(body: bytes, url: str = "", content_type: str = "") -> str:
    head = body[:8]
    u = url.lower().split("?")[0]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK"):
        try:
            names = zipfile.ZipFile(io.BytesIO(body)).namelist()
        except zipfile.BadZipFile:
            return "unknown"
        if any(n.startswith("xl/") for n in names):
            return "xlsx"
        if any(n.startswith("word/") for n in names):
            return "docx"
        return "zip"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "xls"
    if u.endswith((".csv", ".txt")) or "csv" in content_type:
        return "csv" if u.endswith(".csv") or "csv" in content_type else "text"
    if b"<" in body[:2000] and (b"<html" in body[:5000].lower() or b"<table" in body.lower() or "html" in content_type):
        return "html"
    return "text"


def tables_from_xlsx(body: bytes) -> list[list[list[Any]]]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(body), read_only=True, data_only=True)
    return [[list(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets]


def tables_from_xls(body: bytes) -> list[list[list[Any]]]:
    import xlrd
    book = xlrd.open_workbook(file_contents=body)
    out = []
    for sh in book.sheets():
        rows = []
        for i in range(sh.nrows):
            row = []
            for j in range(sh.ncols):
                c = sh.cell(i, j)
                if c.ctype == xlrd.XL_CELL_DATE:
                    row.append(xlrd.xldate.xldate_as_datetime(c.value, book.datemode))
                else:
                    row.append(c.value)
            rows.append(row)
        out.append(rows)
    return out


def tables_from_csv(body: bytes) -> list[list[list[Any]]]:
    text = body.decode("utf-8-sig", "replace")
    try:
        dialect = csv.Sniffer().sniff(text[:5000], delimiters=",\t|;")
    except csv.Error:
        dialect = csv.excel
    return [list(csv.reader(io.StringIO(text), dialect))]


def tables_from_html(body: bytes) -> tuple[list[list[list[Any]]], str]:
    soup = BeautifulSoup(body, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    tables = []
    for tbl in soup.find_all("table"):
        rows = []
        for tr in tbl.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
        if len(rows) >= 2:
            tables.append(rows)
    text = soup.get_text("\n")
    return tables, text


def pdf_tables_and_text(body: bytes, max_pages: int = 300) -> tuple[list[list[list[Any]]], str]:
    import pdfplumber
    tables, lines = [], []
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        # Tables that continue across pages share one header: stitch them together.
        current: list[list[Any]] = []
        for page in pdf.pages[:max_pages]:
            try:
                for t in page.extract_tables():
                    if t and len(t) >= 1:
                        current.extend(t)
            except Exception:  # noqa: BLE001 - pdfplumber can choke on odd pages
                pass
            lines.append(page.extract_text(layout=False) or "")
        if current:
            tables.append(current)
    return tables, "\n".join(lines)


def text_from_docx(body: bytes) -> str:
    xml = zipfile.ZipFile(io.BytesIO(body)).read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    return re.sub(r"<[^>]+>", "", xml)


# --------------------------------------------------------------------------
# Table mode
# --------------------------------------------------------------------------

def map_headers(headers: list[Any]) -> dict[str, int]:
    normed = [norm(h) for h in headers]
    mapping: dict[str, int] = {}
    used: set[int] = set()
    for exact in (True, False):
        for fld, aliases in ALIASES.items():
            if fld in mapping:
                continue
            for alias in aliases:
                for i, h in enumerate(normed):
                    if i in used or not h or len(h) > 60:
                        continue
                    if (h == alias) if exact else re.search(rf"\b{re.escape(alias)}\b", h):
                        mapping[fld] = i
                        used.add(i)
                        break
                if fld in mapping:
                    break
    return mapping


def rows_from_table(table: list[list[Any]]) -> tuple[list[Row], dict]:
    best_i, best_map = None, {}
    for i, r in enumerate(table[:30]):
        m = map_headers(r)
        if "amount" in m and ("name" in m or "case_number" in m or "caption" in m or "parcel" in m):
            if len(m) > len(best_map):
                best_i, best_map = i, m
    if best_i is None:
        return [], {}
    headers = [clean(h) for h in table[best_i]]
    out = []
    for raw in table[best_i + 1:]:
        if not raw or all(c in (None, "") for c in raw):
            continue
        get = lambda f: raw[best_map[f]] if f in best_map and best_map[f] < len(raw) else None  # noqa: E731
        if map_headers(raw) == best_map:   # repeated header row (multi-page tables)
            continue
        joined = " ".join(clean(c) for c in raw)
        if TOTAL_RE.search(joined) and not CASE_RE.search(joined):
            continue
        row = Row(
            case_number=clean(get("case_number")), name=clean(get("name")), plaintiff=clean(get("plaintiff")),
            amount=parse_money(get("amount")), date=parse_date(get("date")), parcel=clean(get("parcel")),
            address=clean(get("address")), mailing=clean(get("mailing")), caption=clean(get("caption")),
            raw=joined[:400], method="table",
        )
        if not (row.case_number or row.name or row.caption or row.parcel):
            continue
        out.append(row)
    return out, {k: headers[v] for k, v in best_map.items() if v < len(headers)}


# --------------------------------------------------------------------------
# Line mode
# --------------------------------------------------------------------------
LEGALESE_RE = re.compile(r"\b(pursuant|revised code|section \d|hereby|notified|o\.?r\.?c\.?|statute|chapter \d)\b", re.I)
NAME_JUNK_RE = re.compile(r"\b(excess|surplus|funds|proceeds|amount|case|parcel|total|page|report|"
                          r"county|clerk|treasurer|sheriff|list|balance|date|sale)\b", re.I)


def row_from_line(line: str) -> Optional[Row]:
    line = clean(line)
    if len(line) < 8 or TOTAL_RE.search(line):
        return None
    if LEGALESE_RE.search(line):
        return None
    monies = []
    for m in MONEY_RE.finditer(line):
        tok = m.group(0)
        bare = "$" not in tok and "," not in tok
        if bare and line[m.end():].strip():      # bare "123.45" only counts at the end of the line
            continue
        monies.append(tok)
    if not monies:
        return None
    amount = parse_money(monies[-1])          # last amount on the line is usually the balance
    if amount is None or amount <= 0:
        return None
    case = CASE_RE.search(line)
    parcel = PARCEL_RE.search(line)
    dt = DATE_RE.search(line)
    addr = ADDRESS_RE.search(line)
    rest = line
    for piece in [*monies, case.group(0) if case else "", parcel.group(0) if parcel else "",
                  dt.group(0) if dt else "", addr.group(0) if addr else ""]:
        if piece:
            rest = rest.replace(piece, " ")
    def tidy(t: str) -> str:
        t = re.sub(r"[|$#:;]+", " ", t)
        return re.sub(r"\s+", " ", t).strip(" ,.-")
    rest = tidy(rest)
    if addr and len(re.findall(r"[A-Za-z]{2,}", rest)) < 2:
        # the "address" was really part of the name (e.g. "8724 MUNSON ROAD LLC")
        rest = line
        for piece in [*monies, case.group(0) if case else "", dt.group(0) if dt else ""]:
            if piece:
                rest = tidy(rest.replace(piece, " "))
        addr = None
    name = rest if len(re.findall(r"[A-Za-z]{2,}", rest)) >= 2 else ""
    if name and len(NAME_JUNK_RE.findall(name)) >= 2:
        name = ""
    if not (name or case):
        return None
    return Row(
        case_number=case.group(0) if case else "", name=name[:120], amount=amount,
        date=parse_date(dt.group(0)) if dt else "", parcel=parcel.group(0) if parcel and not case else "",
        address=addr.group(0) if addr else "", raw=line[:400], method="text",
    )


def rows_from_text(text: str) -> list[Row]:
    out = []
    for line in text.splitlines():
        r = row_from_line(line)
        if r:
            out.append(r)
    return out


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def extract(body: bytes, url: str = "", content_type: str = "") -> Extraction:
    fmt = detect_format(body, url, content_type)
    tables: list[list[list[Any]]] = []
    text = ""
    try:
        if fmt == "xlsx":
            tables = tables_from_xlsx(body)
        elif fmt == "xls":
            tables = tables_from_xls(body)
        elif fmt == "csv":
            tables = tables_from_csv(body)
        elif fmt == "html":
            tables, text = tables_from_html(body)
        elif fmt == "pdf":
            tables, text = pdf_tables_and_text(body)
        elif fmt == "docx":
            text = text_from_docx(body)
        elif fmt == "text":
            text = body.decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return Extraction(format=fmt, note=f"read error: {exc}")

    best = Extraction(format=fmt)
    for t in tables:
        rows, cols = rows_from_table(t)
        cand = Extraction(rows=rows, method="table", format=fmt, columns=cols)
        if len(cand.rows) * cand.quality > len(best.rows) * best.quality:
            best = cand
    if text and (not best.rows or best.quality < 0.5):
        cand = Extraction(rows=rows_from_text(text), method="text", format=fmt)
        if len(cand.rows) * cand.quality > len(best.rows) * best.quality * 1.2:
            best = cand
    if fmt in ("xlsx", "xls", "csv") and not best.rows and tables:
        # spreadsheet without a recognizable header: treat each row as a line
        lines = [" | ".join(clean(c) for c in r if c not in (None, "")) for t in tables for r in t]
        cand = Extraction(rows=rows_from_text("\n".join(lines)), method="text", format=fmt)
        best = cand if cand.rows else best
    return best
