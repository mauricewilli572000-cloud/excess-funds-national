"""
Cuyahoga County Clerk of Courts docket session handling.

Target: https://cpdocket.cp.cuyahogacounty.gov/Search.aspx

Every constant and parsing routine in this file has been confirmed against
real live responses (see README.md "Status" section and src/discover_*.py).
Nothing here is a guess.
"""

import re
from dataclasses import dataclass, field
from typing import Optional, List, Tuple
from urllib.parse import unquote, urljoin

import requests
from bs4 import BeautifulSoup

# Matches an MS AJAX "pageRedirect" delta record: length|pageRedirect|id|url|
# The Check.aspx hop (confirmed not a CAPTCHA - see README) can arrive this
# way, embedded in an async delta with the real HTTP response status still
# 200 and resp.url unchanged, instead of as an HTTP-level redirect. Matched
# directly against the raw text rather than via parse_async_delta's dict,
# since multiple non-updatePanel records sharing an empty id otherwise
# collide in that dict (last-content-wins via setdefault) and silently
# drop this.
_PAGE_REDIRECT_RE = re.compile(r"\|pageRedirect\|[^|]*\|([^|]*)\|")

BASE_URL = "https://cpdocket.cp.cuyahogacounty.gov"
SEARCH_URL = f"{BASE_URL}/Search.aspx"
TOS_URL = f"{BASE_URL}/TOS.aspx"
CHECK_URL = f"{BASE_URL}/Check.aspx"

UPDATE_PANEL_ID = "ctl00$SheetContentPlaceHolder$UpdatePanel1"

# --- Search type selection (AJAX partial postback) ---
# Confirmed via discover_live_form_fields.py against the live page
# (2026-10-13): each search-type radio is its own <input type="radio"
# name="ctl00$SheetContentPlaceHolder$rbSearches" value="...">, not a
# "rblSearchType" field (that name doesn't exist anywhere on the page).
# Posting the wrong field name here meant the server never saw "forcl"
# selected, silently fell back to whatever radio was already checked
# (observed as rbSearches=coaame, i.e. "Court of Appeals Search by
# Name"), and rendered THAT panel - which has none of the
# foreclosureSearch$ fields, explaining both the "missing" fields and
# the downstream 500 when submit_foreclosure_search() then posted
# against controls that were never part of that page's control tree.
FORECLOSURE_EVENT_TARGET = "ctl00$SheetContentPlaceHolder$rbCivilForeclosure"
SEARCH_TYPE_RADIO_NAME = "ctl00$SheetContentPlaceHolder$rbSearches"
SEARCH_TYPE_FORECLOSURE_VALUE = "forcl"

# --- Terms of Service (plain Yes/No button pair, not a checkbox) ---
TOS_ACCEPT_FIELD = "ctl00$SheetContentPlaceHolder$btnYes"
TOS_ACCEPT_VALUE = "Yes"

# --- Foreclosure search form field prefix ---
FORECLOSURE_PANEL_PREFIX = "ctl00$SheetContentPlaceHolder$foreclosureSearch$"

FIELD_PARCEL_NBR = FORECLOSURE_PANEL_PREFIX + "txtParcelNbr"
FIELD_CASE_YEAR = FORECLOSURE_PANEL_PREFIX + "ddlCaseYear"
FIELD_CASE_SEQUENCE = FORECLOSURE_PANEL_PREFIX + "txtCaseSequence"
FIELD_FILING_TYPE = FORECLOSURE_PANEL_PREFIX + "ddlFilingType"
FIELD_FROM_DATE = FORECLOSURE_PANEL_PREFIX + "txtFromDate"
FIELD_TO_DATE = FORECLOSURE_PANEL_PREFIX + "txtToDate"
FIELD_ACTIVE_ONLY = FORECLOSURE_PANEL_PREFIX + "cbStatus"
FIELD_EXCLUDE_BR = FORECLOSURE_PANEL_PREFIX + "cbBRCases"
FIELD_STREET_NBR = FORECLOSURE_PANEL_PREFIX + "txtStreetNbr"
FIELD_STREET_NAME = FORECLOSURE_PANEL_PREFIX + "txtStreetName"
FIELD_CITY = FORECLOSURE_PANEL_PREFIX + "txtCity"
FIELD_ZIP = FORECLOSURE_PANEL_PREFIX + "txtZip"
FIELD_BANKRUPTCY_STAY = FORECLOSURE_PANEL_PREFIX + "cbBankruptcyStay"
FIELD_SUBMIT = FORECLOSURE_PANEL_PREFIX + "btnSubmit"
FIELD_SUBMIT_VALUE = "Submit Search"
FIELD_CLEAR = FORECLOSURE_PANEL_PREFIX + "btnClear"

# Filing type codes (ddlFilingType values), confirmed from discover_foreclosure_form.py
FILING_TYPE_CODES = {
    "mortgage_foreclosure": "1460",
    "tax_foreclosure": "1465",
    "hoa_lien_foreclosure": "1466",
    "mechanics_lien_foreclosure": "1467",
    "land_contract_forfeiture": "1470",
    "other_foreclosure": "1480",
}

# --- Case detail page tabs (__doPostBack targets) ---
CASE_TAB_PREFIX = "ctl00$SheetContentPlaceHolder$caseHeader$"
CASE_TAB_SUMMARY = CASE_TAB_PREFIX + "lbSummary"
CASE_TAB_DOCKET = CASE_TAB_PREFIX + "lbDocket"
CASE_TAB_PARTIES = CASE_TAB_PREFIX + "lbParties"
CASE_TAB_COSTS = CASE_TAB_PREFIX + "lbCosts"
CASE_TAB_SERVICE = CASE_TAB_PREFIX + "lbService"
CASE_TAB_PROPERTY = CASE_TAB_PREFIX + "lbProperty"
CASE_TAB_ALL = CASE_TAB_PREFIX + "lbAll"

# Confirmed docket description pattern for the original complaint filing
_COMPLAINT_DOCKET_RE = re.compile(r"(?i)case filed.*complaint")
_PDF_LINK_RE = re.compile(r"(?i)\.pdf(\?|$)")


def parse_async_delta(response_text: str) -> dict:
    """
    Parse an MS AJAX UpdatePanel partial-postback "delta" response.

    Format is a sequence of pipe-delimited records:
        length|type|id|content|length|type|id|content|...
    where `content` is exactly `length` characters and may itself contain
    literal `|` characters, so this must walk by declared length rather
    than naive str.split("|").

    Returns {id: content} for all records of type "updatePanel", plus
    special keys "__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"
    when present (type "hiddenField").
    """
    result: dict = {}
    pos = 0
    n = len(response_text)
    while pos < n:
        bar1 = response_text.find("|", pos)
        if bar1 == -1:
            break
        length_str = response_text[pos:bar1]
        if not length_str.isdigit():
            # Not a well-formed record start; bail out defensively.
            break
        length = int(length_str)
        bar2 = response_text.find("|", bar1 + 1)
        bar3 = response_text.find("|", bar2 + 1)
        if bar2 == -1 or bar3 == -1:
            break
        rec_type = response_text[bar1 + 1:bar2]
        rec_id = response_text[bar2 + 1:bar3]
        content_start = bar3 + 1
        content = response_text[content_start:content_start + length]
        pos = content_start + length
        if pos < n and response_text[pos] == "|":
            pos += 1

        if rec_type == "hiddenField":
            result[rec_id] = content
        elif rec_type in ("updatePanel",):
            result[rec_id] = content
        else:
            # updatePanel data, asyncPostBackControlIDs, etc. - keep by id anyway
            result.setdefault(rec_id, content)
    return result


@dataclass
class CaseListing:
    case_number: str
    case_detail_event_target: str
    case_detail_event_argument: str = ""
    defendant: str = ""
    property_address: str = ""
    property_city: str = ""
    property_zip: str = ""
    parcel_number: str = ""
    status: str = ""
    filed_date: str = ""
    party_summary: str = ""


@dataclass
class CaseSummary:
    case_number: str = ""
    case_title: str = ""
    plaintiff: str = ""
    defendant: str = ""
    case_designation: str = ""
    filing_date: str = ""
    judge: str = ""
    last_status: str = ""
    last_status_date: str = ""
    prayer_amount: Optional[float] = None


@dataclass
class DocketEntry:
    filing_date: str = ""
    docket_party: str = ""
    docket_type: str = ""
    description: str = ""
    image_url: str = ""


@dataclass
class ImageListEntry:
    image_number: str = ""
    description: str = ""
    document_description: str = ""
    doc_url: str = ""


class CuyahogaDocketSession:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0 Safari/537.36",
        })
        self.viewstate = ""
        self.viewstategenerator = ""
        self.eventvalidation = ""
        self.current_url = SEARCH_URL

        # Diagnostics from the most recent _async_postback() call - not
        # used for control flow, just surfaced so run_pipeline.py's debug
        # file can show *why* a postback came back empty instead of only
        # *that* it did.
        self.last_postback_status = None
        self.last_postback_delta_keys: list = []
        self.last_postback_raw_head = ""

    # ------------------------------------------------------------------
    # Core helpers
    # ------------------------------------------------------------------
    def _extract_viewstate(self, html: str) -> None:
        soup = BeautifulSoup(html, "html.parser")

        def _val(name):
            tag = soup.find("input", {"name": name})
            return tag["value"] if tag and tag.has_attr("value") else ""

        self.viewstate = _val("__VIEWSTATE")
        self.viewstategenerator = _val("__VIEWSTATEGENERATOR")
        self.eventvalidation = _val("__EVENTVALIDATION")

    def _apply_delta_viewstate(self, delta: dict) -> None:
        if "__VIEWSTATE" in delta:
            self.viewstate = delta["__VIEWSTATE"]
        if "__VIEWSTATEGENERATOR" in delta:
            self.viewstategenerator = delta["__VIEWSTATEGENERATOR"]
        if "__EVENTVALIDATION" in delta:
            self.eventvalidation = delta["__EVENTVALIDATION"]

    def load_search_page(self) -> str:
        """GET the search page; auto-accept TOS if presented."""
        resp = self.session.get(SEARCH_URL)
        resp.raise_for_status()
        html = resp.text
        self.current_url = resp.url

        if "TOS.aspx" in resp.url or "Terms of Service" in html:
            html = self._accept_terms_of_service(html)

        self._extract_viewstate(html)
        return html

    def _accept_terms_of_service(self, html: str) -> str:
        self._extract_viewstate(html)
        payload = {
            "__VIEWSTATE": self.viewstate,
            "__VIEWSTATEGENERATOR": self.viewstategenerator,
            "__EVENTVALIDATION": self.eventvalidation,
            TOS_ACCEPT_FIELD: TOS_ACCEPT_VALUE,
        }
        resp = self.session.post(self.current_url or TOS_URL, data=payload)
        resp.raise_for_status()
        self.current_url = resp.url
        self._extract_viewstate(resp.text)
        return resp.text

    def _async_postback(self, event_target: str, extra_fields: Optional[dict] = None,
                         event_argument: str = "") -> Tuple[str, bool]:
        """
        Shared helper for AJAX UpdatePanel partial postbacks.

        Returns (html_or_delta_content, was_async). Handles:
          - the normal pipe-delimited delta response
          - a full-page fallback (non-delta response)
          - TOS redirect if the session had expired
          - the Check.aspx hop after a search submit (confirmed not a
            CAPTCHA - just an intermediate redirect), followed
            automatically with a GET + Referer.
        """
        payload = {
            "__EVENTTARGET": event_target,
            "__EVENTARGUMENT": event_argument,
            "__VIEWSTATE": self.viewstate,
            "__VIEWSTATEGENERATOR": self.viewstategenerator,
            "__EVENTVALIDATION": self.eventvalidation,
            "__ASYNCPOST": "true",
            UPDATE_PANEL_ID: UPDATE_PANEL_ID,
        }
        if extra_fields:
            payload.update(extra_fields)

        headers = {
            "X-MicrosoftAjax": "Delta=true",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.current_url,
        }

        resp = self.session.post(self.current_url, data=payload, headers=headers)
        resp.raise_for_status()

        if "TOS.aspx" in resp.url or "Terms of Service" in resp.text:
            self._accept_terms_of_service(resp.text)
            # Retry once after accepting TOS.
            resp = self.session.post(self.current_url, data=payload, headers=headers)
            resp.raise_for_status()

        text = resp.text
        is_delta = "|updatePanel|" in text or text.startswith(tuple("0123456789"))
        self.last_postback_status = resp.status_code
        self.last_postback_raw_head = text[:500]

        if is_delta:
            delta = parse_async_delta(text)
            self._apply_delta_viewstate(delta)
            self.last_postback_delta_keys = list(delta.keys())
            content = "\n".join(v for k, v in delta.items()
                                 if k not in ("__VIEWSTATE", "__VIEWSTATEGENERATOR",
                                               "__EVENTVALIDATION"))
        else:
            self._extract_viewstate(text)
            self.last_postback_delta_keys = []
            content = text

        # Follow the Check.aspx hop if we land there (confirmed not a CAPTCHA).
        # Two different ways this shows up: a real HTTP redirect (resp.url
        # changes), or - as seen once the search-type radio fix let the
        # submit postback actually reach the real foreclosure panel - a
        # pageRedirect record embedded in the async delta itself, with
        # resp.url staying put at Search.aspx.
        redirect_url = None
        if "Check.aspx" in resp.url:
            redirect_url = resp.url
        elif is_delta:
            m = _PAGE_REDIRECT_RE.search(text)
            if m:
                redirect_url = urljoin(self.current_url, unquote(m.group(1)))

        if redirect_url:
            follow = self.session.get(redirect_url, headers={"Referer": self.current_url})
            follow.raise_for_status()
            self.current_url = follow.url
            self._extract_viewstate(follow.text)
            return follow.text, False

        self.current_url = resp.url
        return content, is_delta

    def _postback_current_page(self, event_target: str, event_argument: str = "") -> str:
        """Plain synchronous __doPostBack (no UpdatePanel on this page)."""
        payload = {
            "__EVENTTARGET": event_target,
            "__EVENTARGUMENT": event_argument,
            "__VIEWSTATE": self.viewstate,
            "__VIEWSTATEGENERATOR": self.viewstategenerator,
            "__EVENTVALIDATION": self.eventvalidation,
        }
        resp = self.session.post(self.current_url, data=payload,
                                  headers={"Referer": self.current_url})
        resp.raise_for_status()

        if "Check.aspx" in resp.url:
            follow = self.session.get(resp.url, headers={"Referer": self.current_url})
            follow.raise_for_status()
            resp = follow

        self.current_url = resp.url
        self._extract_viewstate(resp.text)
        return resp.text

    # ------------------------------------------------------------------
    # Search flow
    # ------------------------------------------------------------------
    def select_foreclosure_search(self) -> str:
        content, _ = self._async_postback(
            FORECLOSURE_EVENT_TARGET,
            extra_fields={SEARCH_TYPE_RADIO_NAME: SEARCH_TYPE_FORECLOSURE_VALUE},
        )
        return content

    def submit_foreclosure_search(
        self,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        *,
        parcel_number: str = "",
        street_number: str = "",
        street_name: str = "",
        city: str = "",
        zip_code: str = "",
        active_only: bool = False,
        exclude_br_cases: bool = False,
        filing_type: str = "",
    ) -> Tuple[str, bool]:
        extra = {
            FIELD_PARCEL_NBR: parcel_number,
            FIELD_STREET_NBR: street_number,
            FIELD_STREET_NAME: street_name,
            FIELD_CITY: city,
            FIELD_ZIP: zip_code,
            FIELD_FROM_DATE: date_from or "",
            FIELD_TO_DATE: date_to or "",
            FIELD_FILING_TYPE: filing_type,
            FIELD_SUBMIT: FIELD_SUBMIT_VALUE,
        }
        if active_only:
            extra[FIELD_ACTIVE_ONLY] = "on"
        if exclude_br_cases:
            extra[FIELD_EXCLUDE_BR] = "on"

        # Tried leaving __EVENTTARGET empty here (treating Submit as a
        # plain button rather than an AutoPostBack control) - that came
        # back as an explicit MS AJAX error delta ("0|error|500||", see
        # submit_postback_raw_head in last_run_debug.json), i.e. a real
        # server-side exception, which is worse evidence than the silent
        # empty delta this __EVENTTARGET=FIELD_SUBMIT version produces.
        # Reverted. The postback_status/delta_keys/raw_head diagnostics
        # stay on so the next run shows what THIS version's raw response
        # actually looks like (not captured before this debugging pass).
        html, was_async = self._async_postback(FIELD_SUBMIT, extra_fields=extra)
        self._save_results_state()
        return html, was_async

    # ------------------------------------------------------------------
    # Results-page state
    #
    # Opening a case moves the session onto that case's page (new URL and
    # viewstate). Each later case link only exists on the RESULTS page, so
    # it has to be posted from the results page's own URL/viewstate. Without
    # this, every case after the first was "opened" from the previous case's
    # page, and the site just re-served that same case - so every lead
    # inherited the first case's plaintiff and amount.
    # ------------------------------------------------------------------
    def _save_results_state(self) -> None:
        self._results_state = (
            self.current_url,
            self.viewstate,
            self.viewstategenerator,
            self.eventvalidation,
        )

    def _restore_results_state(self) -> bool:
        state = getattr(self, "_results_state", None)
        if not state:
            return False
        (self.current_url, self.viewstate,
         self.viewstategenerator, self.eventvalidation) = state
        return True

    @staticmethod
    def _norm_case(value: str) -> str:
        return re.sub(r"[^A-Z0-9]", "", (value or "").upper())

    def _case_page_matches(self, html: str, listing: "CaseListing") -> bool:
        """True unless the page names a DIFFERENT case than the one asked for.
        (If the page has no readable case number we can't tell, so allow it.)"""
        page_case = self._norm_case(parse_case_summary(html).case_number)
        want = self._norm_case(listing.case_number)
        return not (page_case and want and page_case != want)

    def open_case_detail(self, listing: CaseListing) -> str:
        restored = self._restore_results_state()
        html = self._postback_current_page(
            listing.case_detail_event_target,
            listing.case_detail_event_argument,
        )
        if self._case_page_matches(html, listing):
            return html

        # Wrong case came back: reload the results page for fresh state and
        # try once more before giving up.
        state = getattr(self, "_results_state", None)
        if state:
            try:
                resp = self.session.get(state[0], headers={"Referer": state[0]})
                resp.raise_for_status()
                self.current_url = resp.url
                self._extract_viewstate(resp.text)
                html = self._postback_current_page(
                    listing.case_detail_event_target,
                    listing.case_detail_event_argument,
                )
                if self._case_page_matches(html, listing):
                    return html
            except Exception:
                pass

        # Refuse to return another case's page - a failed lead is visible in
        # case_detail_failures, a wrong one silently corrupts the data.
        raise RuntimeError(
            f"Opened the wrong case page for {listing.case_number} "
            f"(results state restored: {restored})"
        )

    def click_case_tab(self, event_target: str) -> str:
        return self._postback_current_page(event_target)

    def search_foreclosures(self, date_from: str, date_to: str) -> Tuple["CuyahogaDocketSession", List[CaseListing]]:
        self.load_search_page()
        self.select_foreclosure_search()
        html, _ = self.submit_foreclosure_search(date_from=date_from, date_to=date_to)
        listings = parse_results_grid(html)
        return self, listings


# ----------------------------------------------------------------------
# Parsing routines
# ----------------------------------------------------------------------

_DOPOSTBACK_RE = re.compile(
    r"__doPostBack\('([^']+)'\s*,\s*'([^']*)'\)"
)


def _find_results_table(soup: BeautifulSoup):
    confirmed_id = "SheetContentPlaceHolder_ctl00_gvForeclosureResults"
    table = soup.find("table", id=confirmed_id)
    if table:
        return table
    # Fallback: generic grid/results id pattern.
    table = soup.find("table", id=re.compile(r"(?i)(gvForeclosure|gvResults|grid)"))
    if table:
        return table
    # Last resort: largest table that contains doPostBack links.
    candidates = soup.find_all("table")
    best = None
    best_score = -1
    for t in candidates:
        links = t.find_all("a", href=re.compile(r"__doPostBack"))
        score = len(links)
        if score > best_score:
            best_score = score
            best = t
    return best


def parse_results_grid(html: str) -> List[CaseListing]:
    soup = BeautifulSoup(html, "html.parser")
    table = _find_results_table(soup)
    if table is None:
        return []

    rows = table.find_all("tr")
    if not rows:
        return []

    listings: List[CaseListing] = []
    for row in rows[1:]:  # skip header row
        cells = row.find_all("td")
        if len(cells) < 8:
            continue

        defendant_cell, addr_cell, city_cell, zip_cell, case_cell, parcel_cell, status_cell, filed_cell = cells[:8]

        case_number = case_cell.get_text(strip=True)
        event_target, event_argument = "", ""
        link = case_cell.find("a", href=re.compile(r"__doPostBack"))
        if link:
            m = _DOPOSTBACK_RE.search(link["href"])
            if m:
                event_target, event_argument = m.group(1), m.group(2)

        listings.append(CaseListing(
            case_number=case_number,
            case_detail_event_target=event_target,
            case_detail_event_argument=event_argument,
            defendant=defendant_cell.get_text(strip=True),
            property_address=addr_cell.get_text(strip=True),
            property_city=city_cell.get_text(strip=True),
            property_zip=zip_cell.get_text(strip=True),
            parcel_number=parcel_cell.get_text(strip=True),
            status=status_cell.get_text(strip=True),
            filed_date=filed_cell.get_text(strip=True),
        ))

    return listings


def _label_text(soup: BeautifulSoup, span_id: str) -> str:
    tag = soup.find(id=span_id)
    return tag.get_text(strip=True) if tag else ""


def parse_case_summary(html: str) -> CaseSummary:
    soup = BeautifulSoup(html, "html.parser")
    prefix = "SheetContentPlaceHolder_caseSummary_"

    case_number = _label_text(soup, prefix + "lblCaseNumber")
    case_title = _label_text(soup, prefix + "lblCaseTitle")
    case_designation = _label_text(soup, prefix + "lblCaseDesignation")
    filing_date = _label_text(soup, prefix + "lblFilingDate")
    judge = _label_text(soup, prefix + "lblJudge")
    last_status = _label_text(soup, prefix + "lblLastStatus")
    last_status_date = _label_text(soup, prefix + "lblLastStatusDate")
    prayer_raw = _label_text(soup, prefix + "lblPrayerAmt")

    plaintiff, defendant = "", ""
    if case_title:
        for sep in (" vs. ", " vs ", " VS. ", " VS "):
            if sep in case_title:
                plaintiff, defendant = case_title.split(sep, 1)
                break
        else:
            plaintiff = case_title

    prayer_amount = None
    if prayer_raw:
        cleaned = re.sub(r"[^0-9.]", "", prayer_raw)
        try:
            prayer_amount = float(cleaned) if cleaned else None
        except ValueError:
            prayer_amount = None

    return CaseSummary(
        case_number=case_number,
        case_title=case_title,
        plaintiff=plaintiff.strip(),
        defendant=defendant.strip(),
        case_designation=case_designation,
        filing_date=filing_date,
        judge=judge,
        last_status=last_status,
        last_status_date=last_status_date,
        prayer_amount=prayer_amount,
    )


def parse_docket_table(html: str) -> List[DocketEntry]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="SheetContentPlaceHolder_caseDocket_gvDocketInformation")
    if table is None:
        return []

    entries: List[DocketEntry] = []
    rows = table.find_all("tr")
    for row in rows[1:]:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        filing_date, docket_party, docket_type, description = (c.get_text(strip=True) for c in cells[:4])
        image_url = ""
        link = row.find("a", href=re.compile(r"DisplayImageList\.aspx"))
        if link and link.has_attr("href"):
            image_url = link["href"]
            if image_url.startswith("/"):
                image_url = BASE_URL + image_url
            elif not image_url.startswith("http"):
                image_url = f"{BASE_URL}/{image_url}"

        entries.append(DocketEntry(
            filing_date=filing_date,
            docket_party=docket_party,
            docket_type=docket_type,
            description=description,
            image_url=image_url,
        ))

    return entries


def find_complaint_image_url(entries: List[DocketEntry]) -> Optional[str]:
    for entry in entries:
        if entry.image_url and _COMPLAINT_DOCKET_RE.search(entry.description):
            return entry.image_url
    # Fallback: earliest entry that just mentions "complaint".
    for entry in entries:
        if entry.image_url and "complaint" in entry.description.lower():
            return entry.image_url
    return None


def parse_image_list(html: str) -> List[ImageListEntry]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="SheetContentPlaceHolder_displayImage_gvImageList")
    if table is None:
        return []

    entries: List[ImageListEntry] = []
    rows = table.find_all("tr")
    for row in rows[1:]:
        cells = row.find_all("td")
        if len(cells) < 3:
            continue
        image_number, description, doc_description = (c.get_text(strip=True) for c in cells[:3])
        doc_url = ""
        link = row.find("a", href=re.compile(r"i_DisplayDoc\.aspx"))
        if link and link.has_attr("href"):
            doc_url = link["href"]
            if doc_url.startswith("/"):
                doc_url = BASE_URL + doc_url
            elif not doc_url.startswith("http"):
                doc_url = f"{BASE_URL}/{doc_url}"

        entries.append(ImageListEntry(
            image_number=image_number,
            description=description,
            document_description=doc_description,
            doc_url=doc_url,
        ))

    return entries


_NOT_THE_COMPLAINT_RE = re.compile(
    r"(?i)\b(answer|counter\s*-?claim|cross\s*-?claim|summons|motion|dismiss|"
    r"notice|affidavit|judgment|entry|order|reply|praecipe|service|return)\b"
)


def find_complaint_doc_url(entries: List[ImageListEntry]) -> Optional[str]:
    # 1) Exact document type "COMPLAINT" (confirmed wording).
    for entry in entries:
        if entry.document_description.strip().upper() == "COMPLAINT" and entry.doc_url:
            return entry.doc_url
    # 2) Variants such as "COMPLAINT W/ EXHIBITS" or "AMENDED COMPLAINT":
    #    any entry naming a complaint that isn't an answer/motion/etc.
    #    Many cases file the complaint under a variant label, which the
    #    exact match above skipped (~44% of cases in one run).
    for entry in entries:
        label = f"{entry.document_description} {entry.description}"
        if (entry.doc_url and "complaint" in label.lower()
                and not _NOT_THE_COMPLAINT_RE.search(label)):
            return entry.doc_url
    return None


def find_complaint_pdf_links(html: str) -> List[str]:
    """Legacy/general fallback link matcher. Superseded by the precise
    docket -> image-list -> doc-url chain above, kept for diagnostics."""
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        if _PDF_LINK_RE.search(a["href"]) or "DisplayDoc" in a["href"]:
            links.append(a["href"])
    return links
