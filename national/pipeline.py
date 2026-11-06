"""Daily scrape of every usable excess-funds source -> one normalized feed.

Sources come from:
  * data/sources.json      — found by national.discover (status auto-verified / verified)
  * sources_manual.yaml    — your hand-added sources, custom connectors and overrides

For each source: download -> skip if unchanged -> generic extract (or custom
connector) -> normalize to leads -> merge with history -> score.
A source that suddenly returns nothing (or < 30% of last time) keeps its last
good data instead of wiping its leads.

Outputs (dashboard/feeds/):
  index.json           per-state / per-county counts and totals, source stats
  {ST}.json            active leads for one state
  top.json             top 1,000 leads nationwide (combined-dashboard tab reads this)
  sources.json         every discovered source with status (dashboard Sources view)
  {ST}_ghl.csv, top_ghl.csv   GoHighLevel imports (same columns as the county scrapers)

Run:  python -m national.pipeline [--state OH] [--source <id>]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

import yaml

from . import config, extract, http

log = logging.getLogger("pipeline")

GHL_HEADERS = [
    "First Name", "Last Name", "Mailing Address", "Mailing City", "Mailing State",
    "Mailing Zip", "Property Address", "Property City", "Property State", "Property Zip",
    "Lead Type", "Document Type", "Date Filed", "Document Number", "Amount/Debt Owed",
    "Seller Score", "Motivated Seller Flags", "Source", "Public Records URL",
]
FUND_LABEL = {"tax_sale": "Tax Sale Surplus", "foreclosure": "Foreclosure Surplus", "unknown": "Excess Funds"}

ENTITY_RE = re.compile(r"\b(LLC|L\.L\.C|INC|CORP(ORATION)?|LTD|L\.?P|CO\.|COMPANY|TRUST|TRUSTEE|BANK|N\.?A\.?|"
                       r"ASSOCIATION|FSB|MORTGAGE|FINANCIAL|SERVICING|LENDING|CREDIT UNION|HOLDINGS|"
                       r"PROPERTIES|INVESTMENTS|PARTNERS|CHURCH|MINISTRIES|FUND)\b", re.I)
GOV_RE = re.compile(r"treasurer|state of|city of|county of|\bcounty\b|united states|department of|board of|"
                    r"village of|township|internal revenue|attorney general|secretary of|\bhud\b|land bank", re.I)
ESTATE_RE = re.compile(r"\b(ESTATE OF|ESTATE|UNKNOWN HEIRS|HEIRS|DEVISEES|DECEASED|DEC'D|EXECUT(OR|RIX)|"
                       r"ADMINISTRAT(OR|RIX)|FIDUCIARY|SURVIVING SPOUSE)\b", re.I)
VS_RE = re.compile(r"\s+(?:-\s*)?(?:VS?\.?|VERSUS)(?:\s*-)?\s+", re.I)
ETAL_RE = re.compile(r",?\s*\b(ET\s*AL\.?|ET\s*UX\.?|ET\s*VIR\.?)\b.*$", re.I)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def clean_owner(name: str) -> str:
    n = ETAL_RE.sub("", re.sub(r"\s+", " ", name or "").strip())
    n = re.sub(r"\b(A\.?K\.?A\.?|F\.?K\.?A\.?|N\.?K\.?A\.?)\b.*$", "", n, flags=re.I)
    n = re.sub(r"^(THE\s+)?(UNKNOWN\s+)?HEIRS[^A-Z]*(AND\s+DEVISEES\s+)?(OF\s+)?", "", n, flags=re.I)
    n = re.sub(r"^(THE\s+)?ESTATE\s+OF\s+", "", n, flags=re.I)
    n = re.sub(r",?\s*(DECEASED|DEC'D)\b.*$", "", n, flags=re.I)
    return n.strip(" ,;-.")


def norm_key(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


# --------------------------------------------------------------------------
# Lead model
# --------------------------------------------------------------------------

@dataclass
class Lead:
    lead_id: str
    state: str
    county: str
    county_id: str
    fund_type: str
    case_number: str = ""
    parcel: str = ""
    owner: str = ""
    owner_raw: str = ""
    plaintiff: str = ""
    owner_type: str = ""
    amount: Optional[float] = None
    sale_date: str = ""
    prop_address: str = ""
    prop_city: str = ""
    prop_zip: str = ""
    mail_address: str = ""
    mail_city: str = ""
    mail_state: str = ""
    mail_zip: str = ""
    parse_method: str = ""
    source_id: str = ""
    source_url: str = ""
    flags: list[str] = field(default_factory=list)
    score: int = 0
    score_reasons: list[str] = field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""
    status: str = "active"
    removed_date: str = ""


def owner_type(raw: str, owner: str) -> str:
    if ESTATE_RE.search(raw or ""):
        return "estate"
    if GOV_RE.search(owner or ""):
        return "government"
    if ENTITY_RE.search(owner or ""):
        return "entity"
    return "individual" if owner else "unknown"


def rows_to_leads(rows: list[extract.Row], src: dict) -> list[Lead]:
    by_id: dict[str, Lead] = {}
    for r in rows:
        if r.amount is None or r.amount <= 0:
            continue
        raw_name, plaintiff = r.name, r.plaintiff
        if not raw_name and r.caption:
            parts = VS_RE.split(r.caption, maxsplit=1)
            plaintiff, raw_name = (parts[0], parts[1]) if len(parts) == 2 else (plaintiff, r.caption)
        owner = clean_owner(raw_name)
        key = norm_key(r.case_number) or norm_key(r.parcel) or \
            hashlib.sha1(f"{norm_key(owner)}|{r.amount}".encode()).hexdigest()[:10]
        lid = f"{src['county_id']}-{key}"
        lead = by_id.get(lid)
        if lead is None:
            lead = Lead(lead_id=lid, state=src["state"], county=src["county"], county_id=src["county_id"],
                        fund_type=src.get("fund_type") or "unknown", case_number=r.case_number, parcel=r.parcel,
                        owner=owner, owner_raw=raw_name, plaintiff=plaintiff or "", amount=0.0,
                        sale_date=r.date, prop_address=r.address, mail_address=r.mailing,
                        parse_method=r.method, source_id=src["id"], source_url=src["url"])
            by_id[lid] = lead
        elif owner and owner.upper() not in lead.owner.upper():
            lead.owner_raw = f"{lead.owner_raw}; {raw_name}"
        lead.amount = round((lead.amount or 0) + r.amount, 2)
        lead.sale_date = lead.sale_date or r.date
        lead.prop_address = lead.prop_address or r.address
    for lead in by_id.values():
        lead.owner_type = owner_type(f"{lead.owner_raw} {lead.plaintiff}", lead.owner)
        if lead.fund_type == "unknown" and re.search(r"treasurer", lead.plaintiff, re.I):
            lead.fund_type = "tax_sale"
    return list(by_id.values())


# --------------------------------------------------------------------------
# Custom connectors
# --------------------------------------------------------------------------

def connector_json_feed(src: dict) -> list[Lead]:
    """Reads a feed published by one of your per-county scrapers (e.g. the
    franklin-excess-funds repo), which already has addresses matched."""
    f = http.fetch(src["url"], check_robots=False)
    if not f or f.status != 200:
        raise RuntimeError(f"could not load {src['url']}")
    data = json.loads(f.text)
    out = []
    for l in data.get("leads", []):
        cn = l.get("case_number", "")
        lead = Lead(
            lead_id=f"{src['county_id']}-{norm_key(cn)}", state=src["state"], county=src["county"],
            county_id=src["county_id"], fund_type=src.get("fund_type", "foreclosure"),
            case_number=cn, parcel=l.get("parcel_number", ""), owner=l.get("owner", ""),
            owner_raw=l.get("defendants_raw", ""), plaintiff=l.get("plaintiff", ""),
            amount=l.get("amount"), sale_date=l.get("deposit_date") or l.get("sale_date", ""),
            prop_address=l.get("prop_address", ""), prop_city=l.get("prop_city", ""), prop_zip=l.get("prop_zip", ""),
            mail_address=l.get("mail_address", ""), mail_city=l.get("mail_city", ""),
            mail_state=l.get("mail_state", ""), mail_zip=l.get("mail_zip", ""),
            parse_method="connector", source_id=src["id"], source_url=l.get("source_url") or src["url"],
        )
        lead.owner_type = l.get("owner_type") or owner_type(lead.owner_raw, lead.owner)
        out.append(lead)
    return out


CONNECTORS = {"json_feed": connector_json_feed}


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

def load_manual() -> dict:
    if not config.MANUAL_SOURCES_PATH.exists():
        return {"sources": [], "overrides": {}}
    data = yaml.safe_load(config.MANUAL_SOURCES_PATH.read_text(encoding="utf-8")) or {}
    return {"sources": data.get("sources") or [], "overrides": data.get("overrides") or {}}


def active_sources(args) -> list[dict]:
    counties = {c["id"]: c for c in load_json(config.COUNTIES_PATH, [])}
    manual = load_manual()
    skip_auto_counties = {str(s["county_id"]) for s in manual["sources"] if s.get("skip_auto_sources")}
    out = []
    for s in load_json(config.SOURCES_PATH, []):
        o = manual["overrides"].get(s["id"], {})
        s = {**s, **o}
        if s["status"] not in ("auto-verified", "verified") or s["county_id"] in skip_auto_counties:
            continue
        s["type"] = s.get("type", "url")
        out.append(s)
    for m in manual["sources"]:
        cid = str(m["county_id"])
        c = counties.get(cid, {})
        m = {"type": "url", "status": "verified", "fund_type": "unknown", **m, "county_id": cid,
             "state": m.get("state") or c.get("state", ""), "county": m.get("county") or c.get("name", "")}
        m["id"] = m.get("id") or hashlib.sha1(m["url"].encode()).hexdigest()[:12]
        if m.get("status") != "disabled":
            out.append(m)
    # Same URL found twice (discovered + manual): keep the manual one
    seen, uniq = set(), []
    for s in reversed(out):
        if s["url"] in seen:
            continue
        seen.add(s["url"])
        uniq.append(s)
    uniq.reverse()
    if args.state:
        uniq = [s for s in uniq if s["state"] in args.state]
    if args.source:
        uniq = [s for s in uniq if s["id"] in args.source]
    return uniq


def scrape_source(src: dict) -> tuple[list[Lead], dict]:
    snap_path = config.SOURCE_SNAPSHOTS_DIR / f"{src['id']}.json"
    snap = load_json(snap_path, {})
    prev_leads = [Lead(**l) for l in snap.get("leads", [])]
    info = {"id": src["id"], "checked": now_iso()}
    try:
        if src["type"] in CONNECTORS:
            leads = CONNECTORS[src["type"]](src)
            body_hash = hashlib.sha1(json.dumps([asdict(l) for l in leads], sort_keys=True).encode()).hexdigest()
        else:
            f = http.fetch(src["url"])
            if not f or f.status != 200 or not f.body:
                raise RuntimeError(f"HTTP {getattr(f, 'status', 'error')}")
            body_hash = hashlib.sha1(f.body).hexdigest()
            if body_hash == snap.get("hash") and prev_leads:
                info.update(result="unchanged", leads=len(prev_leads))
                return prev_leads, info
            ex = extract.extract(f.body, f.url, f.content_type)
            leads = rows_to_leads(ex.rows, src)
            info.update(method=ex.method, quality=ex.quality)
        if prev_leads and len(leads) < 0.3 * len(prev_leads):
            raise RuntimeError(f"only {len(leads)} leads vs {len(prev_leads)} last time — keeping last good data")
        config.SOURCE_SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(json.dumps({"hash": body_hash, "saved": now_iso(),
                                         "leads": [asdict(l) for l in leads]}), encoding="utf-8")
        info.update(result="ok", leads=len(leads))
        return leads, info
    except Exception as exc:  # noqa: BLE001
        info.update(result="error", error=str(exc)[:300], leads=len(prev_leads))
        return prev_leads, info


# --------------------------------------------------------------------------
# Scoring / history
# --------------------------------------------------------------------------

def score(lead: Lead, today: date) -> None:
    pts, why = 0, []
    a = lead.amount or 0
    for threshold, p in ((50_000, 40), (25_000, 34), (10_000, 27), (5_000, 20), (2_500, 14), (1_000, 8)):
        if a >= threshold:
            pts += p
            why.append(f"${a:,.0f} surplus (+{p})")
            break
    else:
        pts += 2
        why.append(f"Small balance ${a:,.0f} (+2)")
    pts_type = {"individual": 18, "estate": 22, "entity": 5}.get(lead.owner_type, 0)
    pts += pts_type
    why.append({"individual": "Individual owner", "estate": "Estate / heirs", "entity": "LLC / company",
                "government": "Government party", "unknown": "Owner unknown"}[lead.owner_type or "unknown"]
               + f" (+{pts_type})")
    if lead.mail_address:
        pts += 15; why.append("Mailing address on file (+15)")
    elif lead.prop_address:
        pts += 8; why.append("Property address on file (+8)")
    if lead.sale_date:
        try:
            age = (today - date.fromisoformat(lead.sale_date)).days
            if age >= 365:
                pts += 12; why.append(f"Unclaimed {age // 30} months (+12)")
            elif age >= 120:
                pts += 7; why.append(f"Unclaimed {age // 30} months (+7)")
        except ValueError:
            pass
    if lead.fund_type == "tax_sale":
        pts += 5; why.append("Tax sale surplus — owner often unaware (+5)")
    if lead.first_seen and (today - date.fromisoformat(lead.first_seen)).days <= 7:
        pts += 5; why.append("New this week (+5)")
    if lead.parse_method == "text":
        why.append("Parsed from text — spot-check the source")
    lead.score, lead.score_reasons = max(0, min(100, pts)), why

    f = [FUND_LABEL.get(lead.fund_type, "Excess Funds")]
    if a >= 25_000:
        f.append("$25k+ surplus")
    elif a >= 10_000:
        f.append("$10k+ surplus")
    f.append({"individual": "Individual owner", "estate": "Estate / heirs", "entity": "LLC / corp owner",
              "government": "Government party"}.get(lead.owner_type, "Owner unknown"))
    if "Unclaimed 12" in " ".join(why) or any("months (+12)" in w for w in why):
        f.append("Unclaimed 1yr+")
    if lead.mail_state and lead.mail_state != lead.state:
        f.append("Out-of-state owner")
    if lead.first_seen and (today - date.fromisoformat(lead.first_seen)).days <= 7:
        f.append("New this week")
    if lead.parse_method == "text":
        f.append("Parsed from text - verify")
    lead.flags = f


def merge_history(current: list[Lead], scraped_sources: set[str], today: str) -> list[Lead]:
    hist = load_json(config.HISTORY_PATH, {}).get("leads", {})
    known = set(Lead.__dataclass_fields__)
    cur = {}
    for l in current:
        if l.lead_id in cur:   # same case from two sources: keep the richer one
            other = cur[l.lead_id]
            if (bool(l.mail_address), l.amount or 0) <= (bool(other.mail_address), other.amount or 0):
                continue
        cur[l.lead_id] = l
    out = []
    for lid, l in cur.items():
        prev = hist.get(lid, {})
        l.first_seen = prev.get("first_seen") or today
        l.last_seen, l.status, l.removed_date = today, "active", ""
        out.append(l)
    for lid, prev in hist.items():
        if lid in cur:
            continue
        old = Lead(**{k: v for k, v in prev.items() if k in known})
        # Only mark removed if we actually re-read its source this run
        if old.status == "active" and old.source_id in scraped_sources:
            old.status, old.removed_date = "no longer listed", today
        out.append(old)
    return out


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------

COMMON_FIRST = set("""JAMES JOHN ROBERT MICHAEL WILLIAM DAVID RICHARD JOSEPH THOMAS CHARLES CHRISTOPHER DANIEL MATTHEW ANTHONY MARK DONALD STEVEN PAUL ANDREW JOSHUA KENNETH KEVIN BRIAN GEORGE TIMOTHY RONALD EDWARD JASON JEFFREY RYAN JACOB GARY NICHOLAS ERIC JONATHAN STEPHEN LARRY JUSTIN SCOTT BRANDON BENJAMIN SAMUEL GREGORY ALEXANDER FRANK PATRICK RAYMOND JACK DENNIS JERRY TYLER AARON JOSE ADAM NATHAN HENRY DOUGLAS ZACHARY PETER KYLE WALTER ETHAN JEREMY HAROLD KEITH CHRISTIAN ROGER NOAH GERALD CARL TERRY SEAN AUSTIN ARTHUR LAWRENCE JESSE DYLAN BRYAN JOE JORDAN BILLY BRUCE ALBERT WILLIE GABRIEL LOGAN ALAN JUAN WAYNE ROY RALPH RANDY EUGENE VINCENT RUSSELL ELIJAH LOUIS BOBBY PHILIP JOHNNY MARY PATRICIA JENNIFER LINDA ELIZABETH BARBARA SUSAN JESSICA SARAH KAREN LISA NANCY BETTY MARGARET SANDRA ASHLEY KIMBERLY EMILY DONNA MICHELLE CAROL AMANDA DOROTHY MELISSA DEBORAH STEPHANIE REBECCA SHARON LAURA CYNTHIA KATHLEEN AMY ANGELA SHIRLEY ANNA BRENDA PAMELA EMMA NICOLE HELEN SAMANTHA KATHERINE CHRISTINE DEBRA RACHEL CAROLYN JANET CATHERINE MARIA HEATHER DIANE RUTH JULIE OLIVIA JOYCE VIRGINIA VICTORIA KELLY LAUREN CHRISTINA JOAN EVELYN JUDITH MEGAN ANDREA CHERYL HANNAH JACQUELINE MARTHA GLORIA TERESA ANN SARA MADISON FRANCES KATHRYN JANICE JEAN ABIGAIL ALICE JUDY SOPHIA GRACE DENISE AMBER DORIS MARILYN DANIELLE BEVERLY ISABELLA THERESA DIANA NATALIE BRITTANY CHARLOTTE MARIE KAYLA ALEXIS LORI JANE CARLOS LUIS JESUS MIGUEL ANTONIO DARLENE TONYA LATOYA SHANICE TAMIKA KEISHA EDDIE CURTIS LEROY EARL CLARENCE TONY DERRICK MARCUS DARNELL TYRONE REGINALD ANDRE MAURICE TERRENCE CEDRIC""".split())


def split_name(owner: str) -> tuple[str, str]:
    owner = (owner or "").strip()
    if not owner or ENTITY_RE.search(owner) or GOV_RE.search(owner):
        return "", owner
    if "," in owner:
        last, _, first = owner.partition(",")
        return first.strip().title(), last.strip().title()
    owner = re.split(r"\s+(?:&|AND)\s+", owner, maxsplit=1, flags=re.I)[0]   # "SMITH JOHN & MARY" -> first person
    parts = owner.split()
    if len(parts) == 1:
        return "", parts[0].title()
    up = [p.upper().strip(".") for p in parts]
    if up[0] not in COMMON_FIRST and up[1] in COMMON_FIRST:     # "JOHNSON MARK L" (last-first order)
        return " ".join(p.title() for p in parts[1:]), parts[0].title()
    return " ".join(p.title() for p in parts[:-1]), parts[-1].title()


def write_csv(path, leads: list[Lead]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(GHL_HEADERS)
        for l in leads:
            if l.owner_type == "government":
                continue
            first, last = split_name(l.owner)
            w.writerow([first, last, l.mail_address, l.mail_city, l.mail_state, l.mail_zip,
                        l.prop_address, l.prop_city, l.state if l.prop_address else "", l.prop_zip,
                        FUND_LABEL.get(l.fund_type, "Excess Funds"), f"{l.county}, {l.state}",
                        l.sale_date, l.case_number or l.parcel,
                        f"{l.amount:.2f}" if l.amount is not None else "", l.score, "; ".join(l.flags),
                        f"{l.county} {l.state} excess funds list", l.source_url])


def write_outputs(leads: list[Lead], source_info: list[dict]) -> dict:
    config.FEEDS_DIR.mkdir(parents=True, exist_ok=True)
    gen = now_iso()
    today = date.today().isoformat()
    active = sorted([l for l in leads if l.status == "active"], key=lambda l: (-l.score, -(l.amount or 0)))

    by_state: dict[str, list[Lead]] = defaultdict(list)
    for l in active:
        by_state[l.state].append(l)
    for old in config.FEEDS_DIR.glob("??.json"):
        if old.stem not in by_state:
            old.unlink()
    states_idx = {}
    for st, ls in sorted(by_state.items()):
        (config.FEEDS_DIR / f"{st}.json").write_text(json.dumps(
            {"generated_at": gen, "state": st, "leads": [asdict(l) for l in ls]}), encoding="utf-8")
        write_csv(config.FEEDS_DIR / f"{st}_ghl.csv", ls)
        counties = defaultdict(lambda: {"leads": 0, "total": 0.0})
        for l in ls:
            counties[l.county]["leads"] += 1
            counties[l.county]["total"] = round(counties[l.county]["total"] + (l.amount or 0), 2)
        states_idx[st] = {"leads": len(ls), "total": round(sum(l.amount or 0 for l in ls), 2),
                          "counties": dict(sorted(counties.items()))}

    top = active[:1000]
    (config.FEEDS_DIR / "top.json").write_text(json.dumps({
        "generated_at": gen, "total_leads": len(active),
        "total_surplus": round(sum(l.amount or 0 for l in active), 2),
        "leads_with_mailing_address": sum(1 for l in top if l.mail_address),
        "new_this_run": sum(1 for l in active if l.first_seen == today),
        "leads": [asdict(l) for l in top]}), encoding="utf-8")
    write_csv(config.FEEDS_DIR / "top_ghl.csv", top)

    srcs = load_json(config.SOURCES_PATH, [])
    info_by_id = {i["id"]: i for i in source_info}
    manual = load_manual()
    cmap = {c["id"]: c for c in load_json(config.COUNTIES_PATH, [])}
    for s in srcs:
        s.update(manual["overrides"].get(s["id"], {}))
        if s["id"] in info_by_id:
            s["last_scrape"] = info_by_id[s["id"]]
    for m in manual["sources"]:
        mid = m.get("id") or hashlib.sha1(m["url"].encode()).hexdigest()[:12]
        cc = cmap.get(str(m["county_id"]), {})
        srcs.append({"id": mid, "state": m.get("state") or cc.get("state", ""),
                     "county": m.get("county") or cc.get("name", ""),
                     "county_id": str(m["county_id"]), "url": m["url"], "status": m.get("status", "verified"),
                     "fund_type": m.get("fund_type", "unknown"), "via": "manual", "format": m.get("type", "url"),
                     "rows": info_by_id.get(mid, {}).get("leads", 0), "last_scrape": info_by_id.get(mid)})
    (config.FEEDS_DIR / "sources.json").write_text(json.dumps({"generated_at": gen, "sources": srcs}),
                                                    encoding="utf-8")

    disc = load_json(config.DISCOVERY_STATE_PATH, {})
    idx = {
        "generated_at": gen,
        "total_leads": len(active),
        "total_surplus": round(sum(l.amount or 0 for l in active), 2),
        "new_this_run": sum(1 for l in active if l.first_seen == today),
        "states": states_idx,
        "sources": {
            "total": len(srcs),
            "usable": sum(1 for s in srcs if s.get("status") in ("auto-verified", "verified")),
            "candidates": sum(1 for s in srcs if s.get("status") == "candidate"),
            "portals": sum(1 for s in srcs if s.get("status") == "portal"),
            "errors_this_run": sum(1 for i in source_info if i.get("result") == "error"),
        },
        "discovery": {"counties_total": len(load_json(config.COUNTIES_PATH, [])),
                      "counties_crawled": len(disc),
                      "counties_with_usable_source": len({s["county_id"] for s in srcs
                                                          if s.get("status") in ("auto-verified", "verified")})},
    }
    (config.FEEDS_DIR / "index.json").write_text(json.dumps(idx, indent=1), encoding="utf-8")
    config.HISTORY_PATH.write_text(json.dumps({"updated": gen, "leads": {l.lead_id: asdict(l) for l in leads}}),
                                   encoding="utf-8")
    log.info("Wrote feeds: %d active leads in %d states, $%s total", len(active), len(by_state),
             f"{idx['total_surplus']:,.2f}")
    return idx


def run(args) -> dict:
    sources = active_sources(args)
    log.info("Scraping %d usable sources", len(sources))
    all_leads, infos = [], []
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        for src, (leads, info) in zip(sources, pool.map(scrape_source, sources)):
            log.info("%-22s %-3s %-5s %s -> %s", src["county"][:22], src["state"], info["result"], src["url"][:80],
                     info.get("error") or f"{info['leads']} leads")
            all_leads.extend(leads)
            infos.append(info)
    scraped = {i["id"] for i in infos if i["result"] in ("ok", "unchanged")}
    merged = merge_history(all_leads, scraped, date.today().isoformat())
    today = date.today()
    for l in merged:
        score(l, today)
    return write_outputs(merged, infos)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("pdfminer", "pdfplumber", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", nargs="*")
    ap.add_argument("--source", nargs="*")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
