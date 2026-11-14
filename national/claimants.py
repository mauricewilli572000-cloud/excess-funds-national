"""Claimant skip-trace file for surplus funds.

The surplus belongs to the people who owned the property before the sheriff sale,
not to whoever owns it now, so skip tracing by property address finds the wrong
person (the new buyer). This module turns each lead into one row per *claimant*:

  * former owners, split into separate people ("JOHN & MARY SMITH" -> 2 rows)
  * for "UNKNOWN HEIRS OF X" / estates: the decedent's name (so the skip-trace
    service can return relatives), plus the probate fiduciary when the decedent
    appears in our Ohio probate scraper data
  * the property as the person's *former* address, any other mailing address on
    the case, and the current owner's name in a "do not contact" column

Upload claimants_skiptrace.csv to a skip-trace service using its name + address
(person) search, not a property or owner-of-address lookup.
"""
from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path

import requests

from . import config

log = logging.getLogger("claimants")

PROBATE_URL = ("https://raw.githubusercontent.com/mauricewilli572000-cloud/franklin-probate-scraper/"
               "main/docs/data/{county}/records.json")
PROBATE_COUNTIES = {"Franklin County": "franklin", "Cuyahoga County": "cuyahoga",
                    "Summit County": "summit", "Montgomery County": "montgomery"}
MIN_AMOUNT = 1000.0
OUT_CSV = "claimants_skiptrace.csv"

HEIRS_RE = re.compile(r"\b(?:UNK(?:NOWN)?\.?\s+HEIRS?|HEIRS)\b[^A-Z]*(?:ETC\.?\s*)?(?:OF\s+)?(?:THE\s+ESTATE\s+OF\s+)?"
                      r"(?:(?:DEVISEES|LEGATEES|BENEFICIARIES|SPOUSES?|ADMINISTRATORS?|EXECUTORS?)[\s,]*(?:AND\s+)?)*"
                      r"(?:OF\s+)?(?P<name>.+)", re.I)
ESTATE_OF_RE = re.compile(r"\b(?:ESTATE\s+OF|EST\s+OF)\s+(?P<name>.+)", re.I)
NOISE_RE = re.compile(r"\b(ET\s*AL|ETAL|ET\s+UX|ET\s+VIR|DECEASED|DEC'D|DECD|DEC|JOHN\s+DOE|JANE\s+DOE|"
                      r"UNKNOWN\s+SPOUSE.*|UNK\.?\s+SPOUSE.*|NKA|FKA|TRUSTEES?|CO-TRUSTEES?|TR|TRS|"
                      r"OR\s+SUCCESSOR.*|SUCCESSOR\s+TRUSTEE.*|ADMINISTRAT\w*|EXECUT\w*)\b\.?", re.I)
AKA_RE = re.compile(r"\s+(?:AKA|A/K/A|FKA|NKA)\s+.*$", re.I)
SUFFIX_RE = re.compile(r"^(JR|SR|II|III|IV)\.?$", re.I)
ENTITY_RE = re.compile(r"\b(LLC|INC|CORP|COMPANY|BANK|TRUST\b(?!EE)|ASSOCIATION|LTD|LP|PARTNERS|HOLDINGS|"
                       r"PROPERTIES|INVESTMENTS?|REALTY|CHURCH|MINISTR)\b", re.I)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip(" ,;.").upper()


def person(name: str) -> dict | None:
    """'SMITH, JOHN A JR' / 'JOHN A SMITH JR' -> {first, middle, last, suffix}."""
    name = AKA_RE.sub("", _clean(NOISE_RE.sub(" ", name)))
    name = re.sub(r"[^A-Z,' -]", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" ,")
    if (not name or ENTITY_RE.search(name) or len(name.replace(" ", "")) < 3
            or re.search(r"\b(UNK|UNKNOWN|HEIRS?|DEVISEES|LEGATEES|BENEFICIARIES|NAMES?)\b", name)):
        return None
    if "," in name:
        last, _, rest = name.partition(",")
        words = rest.split()
        last_words = last.split()
    else:
        words = name.split()
        if len(words) < 2:
            return None
        from .pipeline import COMMON_FIRST
        core = [w for w in words if not SUFFIX_RE.match(w)]
        # 'HESTON JULIE A' / 'JOHNSON MARK' (auditor style, surname first): first word isn't a
        # common first name but the second is, or the name ends in a lone middle initial.
        if len(core) >= 2 and ((core[0] not in COMMON_FIRST and core[1] in COMMON_FIRST)
                               or (len(core[-1]) == 1 and len(core) == 3)):
            sfx = [w for w in words if SUFFIX_RE.match(w)]
            last_words, words = [core[0]] + sfx, core[1:]
        else:
            last_words = [words.pop()]
            if words and SUFFIX_RE.match(last_words[0]) and len(words) >= 2:   # 'JOHN SMITH JR'
                last_words = [words.pop()] + last_words
    suffix = ""
    for lst in (words, last_words):
        if lst and SUFFIX_RE.match(lst[-1]):
            suffix = lst.pop().rstrip(".")
    if not words or not last_words:
        return None
    return {"first": words[0].title(), "middle": " ".join(words[1:]).title(),
            "last": " ".join(last_words).title(), "suffix": suffix.title()}


def split_people(raw: str) -> list[str]:
    """'JOHN SMITH & MARY SMITH' / 'SMITH, JOHN AND MARY' -> individual name strings."""
    raw = _clean(raw)
    parts = [p.strip() for p in re.split(r"\s*(?:&|\bAND\b|;|/)\s*", raw) if p.strip()]
    out = []
    for i, p in enumerate(parts):
        # 'SMITH, JOHN & MARY' -> second part 'MARY' borrows the first part's surname
        if i and len(p.split()) == 1 and "," in parts[0]:
            p = parts[0].split(",")[0] + ", " + p
        out.append(p)
    return out


def claimants_for(owner_raw: str, owner: str, owner_type: str) -> list[dict]:
    raw = _clean(owner_raw or owner)
    m = HEIRS_RE.search(raw) or ESTATE_OF_RE.search(raw)
    if m or owner_type == "estate":
        name = m.group("name") if m else raw
        dec = person(split_people(name)[0]) if split_people(name) else None
        if not dec:
            return []
        return [dict(dec, role="Heir of deceased owner", decedent=f"{dec['first']} {dec['last']}")]
    out = []
    for i, p in enumerate(split_people(raw)):
        pp = person(p)
        if pp:
            out.append(dict(pp, role="Former owner" if i == 0 else "Co-owner", decedent=""))
    return out


def load_probate() -> dict:
    """(county label, LAST, FIRST) -> probate fiduciary info, from the probate scraper's public data."""
    idx = {}
    for label, key in PROBATE_COUNTIES.items():
        try:
            recs = requests.get(PROBATE_URL.format(county=key), timeout=60).json()
        except Exception as e:  # noqa: BLE001
            log.warning("probate data for %s unavailable: %s", key, e)
            continue
        for r in recs if isinstance(recs, list) else recs.values():
            last, first = _clean(r.get("decedent_last")), _clean(r.get("decedent_first"))
            if last and first:
                idx[(label, last, first)] = r
    log.info("probate index: %d decedents", len(idx))
    return idx


def load_current_owners() -> dict:
    """case key -> current (post-sale) owner name, from the docket/parcel lookup caches."""
    out = {}
    for p in (config.DATA_DIR / "enrich").glob("[0-9]*.json"):
        try:
            cache = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for case, e in cache.items():
            if isinstance(e, dict) and e.get("owner_now") and e.get("still_owner") is False:
                out[re.sub(r"[^A-Z0-9]", "", case.upper())] = e["owner_now"]
    return out


HEADERS = ["First Name", "Middle Name", "Last Name", "Suffix", "Role", "Deceased Owner",
           "Last Known Address", "Last Known City", "Last Known State", "Last Known Zip", "Address Note",
           "Other Address", "Probate Fiduciary", "Fiduciary Address", "Current Owner (do not contact)",
           "County", "State", "Case Number", "Surplus Amount", "Sale Date", "Score", "Lead ID"]


def _street_key(s: str) -> str:
    w = re.sub(r"[^A-Z0-9 ]", " ", (s or "").upper()).split()
    return " ".join(w[:2])


def write(leads: list, feeds_dir: Path) -> dict:
    probate = load_probate()
    current = load_current_owners()
    rows, stats = [], {"leads": 0, "claimants": 0, "heirs": 0, "with_fiduciary": 0}
    for l in leads:
        if l.status != "active" or (l.amount or 0) < MIN_AMOUNT or l.owner_type in ("government", "entity"):
            continue
        people = claimants_for(l.owner_raw, l.owner, l.owner_type)
        if not people:
            continue
        stats["leads"] += 1
        case_key = re.sub(r"[^A-Z0-9]", "", (l.case_number or "").upper())
        now_owner = current.get(case_key, "")
        # The property is the claimant's former home; a separate mailing address on the case is a second lead.
        other = ""
        if l.mail_address and _street_key(l.mail_address) != _street_key(l.prop_address):
            other = " ".join(x for x in (l.mail_address, l.mail_city, l.mail_state, l.mail_zip) if x)
        street, city, state, zip_ = l.prop_address, l.prop_city, l.state, l.prop_zip
        note = "Former residence - sold at sheriff sale; search by NAME + this address" if street else ""
        if not street and other:
            street, city, state, zip_ = l.mail_address, l.mail_city, l.mail_state or l.state, l.mail_zip
            note, other = "Mailing address on the case file", ""
        for p in people:
            fid = probate.get((l.county, p["last"].upper(), p["first"].upper())) if p["decedent"] else None
            f = (fid or {}).get("fiduciary") or {}
            if p["decedent"]:
                stats["heirs"] += 1
            if fid:
                stats["with_fiduciary"] += 1
            rows.append([p["first"], p["middle"], p["last"], p["suffix"], p["role"], p["decedent"],
                         street, city, state, zip_, note, other,
                         f.get("name", "").title(), " ".join(x for x in (f.get("street"), f.get("city"),
                                                                        f.get("state"), f.get("zip")) if x),
                         now_owner.title(), l.county, l.state, l.case_number,
                         f"{l.amount:.2f}" if l.amount is not None else "", l.sale_date, l.score, l.lead_id])
    rows.sort(key=lambda r: (-float(r[18] or 0), r[21]))
    with (feeds_dir / OUT_CSV).open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(HEADERS)
        w.writerows(rows)
    stats["claimants"] = len(rows)
    log.info("Claimant skip-trace file: %s", stats)
    return stats
