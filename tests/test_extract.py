"""Run: python -m pytest tests  (or: python tests/test_extract.py)"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from national import extract  # noqa: E402

FX = Path(__file__).parent / "fixtures"


def _x(name):
    return extract.extract((FX / name).read_bytes(), name)


def test_pdf_notice_text_mode():
    e = _x("lake_notice.pdf")
    assert len(e.rows) == 6 and e.total == 120468.86
    assert any(r.name == "8724 MUNSON ROAD LLC" for r in e.rows)


def test_html_table():
    e = _x("ga_table.html")
    assert e.method == "table" and len(e.rows) == 5 and e.total == 42082.84


def test_csv_tax_deed():
    e = _x("fl_tax_deed.csv")
    assert e.columns["case_number"] == "Tax Deed Number" and e.total == 77734.60


def test_xlsx_with_title_rows():
    e = _x("franklin_like.xlsx")
    assert e.method == "table" and len(e.rows) == 5


def test_line_rules():
    assert extract.row_from_line("Pursuant to Revised Code Section 2329.44 notice") is None
    assert extract.row_from_line("Page 3 of 9 Total $9,999.99") is None
    r = extract.row_from_line("2019-1234  DOE JANE  03/01/2024  1,200.00")
    assert (r.case_number, r.name, r.amount, r.date) == ("2019-1234", "DOE JANE", 1200.0, "2024-03-01")


def test_split_name_columns_and_bond():
    e = _x("cuyahoga_split_names.xlsx")
    assert len(e.rows) == 8 and e.total == 20324.63
    assert e.rows[0].name == "JUANITA GILBERT" and e.rows[0].plaintiff == "WELLS FARGO BANK MINNESOTA"
    assert e.rows[6].name == "STEPHEN R. REGOVICH III"


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
