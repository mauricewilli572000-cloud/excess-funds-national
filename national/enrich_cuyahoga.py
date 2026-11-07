"""Cuyahoga County: look up each excess-funds case on the Clerk's docket to get
the property address + parcel, then the Fiscal Officer parcel record.

Why: the Clerk's excess funds lists have case number, names and amounts but no
addresses, and most cases are years older than what the foreclosure scraper
holds. The docket's foreclosure search accepts a case year + sequence number
and its results grid shows property address, city, zip and parcel.

  python -m national.enrich_cuyahoga [--limit N] [--minutes M] [--case CV23976232 ...]

Results are cached in data/enrich/39035.json (one entry per case, never looked
up twice unless it errored); national.pipeline applies the cache to the leads.
Biggest surplus first, so a partial run still covers the most valuable cases.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from bs4 import BeautifulSoup

from . import config
from .vendor_cuyahoga import auditor_enrichment as fo
from .vendor_cuyahoga import cuyahoga_clerk_search as cs

log = logging.getLogger("enrich_cuyahoga")
COUNTY_ID = "39035"
CACHE_PATH = config.DATA_DIR / "enrich" / f"{COUNTY_ID}.json"
DEBUG_PATH = config.DATA_DIR / "enrich" / f"{COUNTY_ID}_debug.json"
DELAY = 2.0              # seconds between docket requests
# The docket starts returning empty results after ~350 quick searches (then
# recovers after a few hours). An unbroken run of empty grids = throttled,
# not "case not found".
BLOCK_STREAK = 8
RETRY_ERRORS_AFTER = timedelta(days=7)

CASE_RE = re.compile(r"^([A-Z]{2})(\d{2})(\d{3,7})$")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def key(case: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (case or "").upper())


def split_case(case: str) -> Optional[tuple[str, str, str]]:
    """'CV23976232' -> ('CV', '2023', '976232'); 'CV86105891' -> ('CV', '1986', '105891')."""
    m = CASE_RE.match(key(case))
    if not m:
        return None
    yy = int(m.group(2))
    year = 2000 + yy if yy <= (datetime.now().year % 100) + 1 else 1900 + yy
    return m.group(1), str(year), m.group(3)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def year_options(panel_html: str) -> dict[str, str]:
    """Map '2023' -> the dropdown's option value (which may be '2023', '23', or an id)."""
    soup = BeautifulSoup(panel_html, "html.parser")
    sel = soup.find("select", {"name": cs.FIELD_CASE_YEAR})
    out: dict[str, str] = {}
    if not sel:
        return out
    for opt in sel.find_all("option"):
        val, text = (opt.get("value") or "").strip(), opt.get_text(strip=True)
        for cand in (text, val):
            m = re.fullmatch(r"(\d{4})|(\d{2})", cand)
            if m:
                y = m.group(1) or (("20" if int(m.group(2)) < 50 else "19") + m.group(2))
                out.setdefault(y, val)
    return out


def name_tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[A-Z]{3,}", (s or "").upper())
            if t not in {"THE", "AND", "LLC", "INC", "ETAL", "HEIRS", "UNKNOWN", "ESTATE", "TRUST", "TRUSTEE"}}


class Searcher:
    def __init__(self) -> None:
        self.sess: Optional[cs.CuyahogaDocketSession] = None
        self.years: dict[str, str] = {}
        self.debug: dict = {}
        self.need_reload = False
        self.searches_this_session = 0

    def _new_session(self) -> None:
        self.sess = cs.CuyahogaDocketSession()
        self.sess.load_search_page()
        time.sleep(DELAY)

    def search(self, case: str) -> dict:
        parts = split_case(case)
        if not parts:
            return {"status": "bad_case_number"}
        _, year, seq = parts
        for attempt in (1, 2):
            try:
                if self.sess is None or self.searches_this_session >= 200:
                    self._new_session()
                    self.searches_this_session = 0
                elif self.need_reload:
                    self.sess.load_search_page()      # back to the search form, same cookies
                    time.sleep(DELAY)
                self.need_reload = False
                panel = self.sess.select_foreclosure_search()
                time.sleep(DELAY)
                if not self.years:
                    self.years = year_options(panel)
                    self.debug["year_options_sample"] = dict(list(self.years.items())[:5])
                    self.debug["year_options_count"] = len(self.years)
                    if not self.years:
                        self.debug["panel_head"] = panel[:3000]
                yval = self.years.get(year)
                if self.years and yval is None:
                    return {"status": "year_not_searchable", "year": year}
                extra = {
                    cs.FIELD_CASE_YEAR: yval if yval is not None else year,
                    cs.FIELD_CASE_SEQUENCE: seq,
                    cs.FIELD_PARCEL_NBR: "", cs.FIELD_STREET_NBR: "", cs.FIELD_STREET_NAME: "",
                    cs.FIELD_CITY: "", cs.FIELD_ZIP: "", cs.FIELD_FROM_DATE: "", cs.FIELD_TO_DATE: "",
                    cs.FIELD_FILING_TYPE: "", cs.FIELD_SUBMIT: cs.FIELD_SUBMIT_VALUE,
                }
                html, _ = self.sess._async_postback(cs.FIELD_SUBMIT, extra_fields=extra)  # noqa: SLF001
                time.sleep(DELAY)
                listings = cs.parse_results_grid(html)
                if "first_result_head" not in self.debug:
                    self.debug["first_result_head"] = html[:3000]
                    self.debug["first_result_listings"] = len(listings)
                want = key(case)
                hit = next((l for l in listings if key(l.case_number) == want), None)
                # Results page leaves the session off the search form: reload it next time.
                self.need_reload = True
                self.searches_this_session += 1
                if not hit:
                    return {"status": "not_found", "grid_rows": len(listings)}
                return {"status": "found", "prop_address": hit.property_address, "prop_city": hit.property_city,
                        "prop_zip": hit.property_zip, "parcel": hit.parcel_number,
                        "docket_defendant": hit.defendant, "docket_status": hit.status,
                        "docket_filed": hit.filed_date}
            except Exception as exc:  # noqa: BLE001
                log.warning("Search %s attempt %d failed: %s", case, attempt, exc)
                self.sess = None
                time.sleep(5 * attempt)
        return {"status": "error"}


def parcel_owner(entry: dict, owner: str) -> None:
    """Fiscal Officer record for the parcel. Use its tax-bill mailing address only
    if it still names our former owner (after a sheriff sale it's usually the buyer)."""
    if not entry.get("parcel"):
        return
    try:
        info = fo.get_parcel_info(entry["parcel"])
    except Exception as exc:  # noqa: BLE001
        entry["parcel_error"] = str(exc)[:200]
        return
    if not info:
        return
    entry["owner_now"] = info.get("owner_name", "")
    entry["owner_now_mailing"] = info.get("mailing_address", "")
    ours, theirs = name_tokens(owner), name_tokens(entry["owner_now"])
    entry["still_owner"] = bool(ours and theirs and ours & theirs)


def pending_cases(cache: dict, only: Optional[list[str]]) -> list[dict]:
    feed = load_json(config.FEEDS_DIR / "OH.json", {}).get("leads", [])
    leads = [l for l in feed if l.get("county_id") == COUNTY_ID and l.get("case_number")]
    if only:
        wanted = {key(c) for c in only}
        return [l for l in leads if key(l["case_number"]) in wanted]
    cutoff = datetime.now(timezone.utc) - RETRY_ERRORS_AFTER
    out = []
    for l in leads:
        c = cache.get(key(l["case_number"]))
        if c and not (c.get("status") == "error" and datetime.fromisoformat(c["looked_up"]) < cutoff):
            continue
        out.append(l)
    out.sort(key=lambda l: -(l.get("amount") or 0))
    return out


def run(limit: int, minutes: float, only: Optional[list[str]]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cache = load_json(CACHE_PATH, {})
    todo = pending_cases(cache, only)[:limit]
    log.info("%d Cuyahoga cases to look up this run (%d already cached)", len(todo), len(cache))
    s = Searcher()
    deadline = time.monotonic() + minutes * 60
    counts: dict[str, int] = {}
    empty_streak: list[str] = []     # case keys in the current run of empty results
    cooled_down = False
    for i, l in enumerate(todo, 1):
        if time.monotonic() > deadline:
            log.info("Time budget reached after %d cases", i - 1)
            break
        entry = s.search(l["case_number"])
        entry["looked_up"] = now_iso()
        if entry["status"] == "not_found" and entry.get("grid_rows") == 0:
            empty_streak.append(key(l["case_number"]))
        elif entry["status"] == "found":
            empty_streak = []
        if len(empty_streak) >= BLOCK_STREAK:
            # Throttled: forget this streak's "not found" results so they get retried.
            for k in empty_streak:
                cache.pop(k, None)
            counts["not_found"] = max(0, counts.get("not_found", 0) - (len(empty_streak) - 1))
            empty_streak = []
            if cooled_down:
                log.warning("Docket still returning empty results after a cool-down — stopping this run; "
                            "the next scheduled run continues where this left off.")
                s.debug["stopped_throttled_at"] = now_iso()
                break
            log.warning("Docket returning empty results (throttled?) — cooling down 10 minutes")
            s.sess = None
            time.sleep(600)
            cooled_down = True
            continue
        if entry["status"] == "found":
            parcel_owner(entry, l.get("owner", ""))
        cache[key(l["case_number"])] = entry
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        if i <= 3 or i % 50 == 0:
            log.info("[%d/%d] %s $%s -> %s %s", i, len(todo), l["case_number"], l.get("amount"),
                     entry["status"], entry.get("prop_address", ""))
        if i % 25 == 0:
            CACHE_PATH.write_text(json.dumps(cache, indent=0), encoding="utf-8")
        if i == 10 and counts.get("found", 0) == 0:
            log.error("First 10 lookups found nothing — stopping so the form can be checked. See %s", DEBUG_PATH)
            break
    CACHE_PATH.write_text(json.dumps(cache, indent=0), encoding="utf-8")
    s.debug.update({"run": now_iso(), "counts": counts, "cached_total": len(cache)})
    DEBUG_PATH.write_text(json.dumps(s.debug, indent=1), encoding="utf-8")
    log.info("Done: %s; cache now %d cases", counts, len(cache))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--minutes", type=float, default=100)
    ap.add_argument("--case", nargs="*")
    a = ap.parse_args()
    run(a.limit, a.minutes, a.case)


if __name__ == "__main__":
    main()
