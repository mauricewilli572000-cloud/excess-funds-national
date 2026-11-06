"""Auto-discover excess funds / surplus lists for every US county.

For each county (oldest-checked first, COUNTIES_PER_RUN per run):
  1. Seed URLs: its registered .gov domains (data/counties.json), else guessed
     domains whose homepage actually mentions the county.
  2. sitemap.xml + on-site search ("excess funds", "surplus funds") +
     a keyword-prioritized crawl (MAX_PAGES_PER_COUNTY pages). Links whose text
     or URL mention excess/surplus/overbid are followed first; links to the
     clerk / treasurer / sheriff / tax collector are followed next — even onto
     their separate domains. "Surplus property" auctions etc. are ignored.
  3. If the crawl finds nothing, a web search (see search_providers.py).
  4. Every candidate list is downloaded and run through the generic extractor.
     Lists that parse into >= AUTO_VERIFY_MIN_ROWS rows with names + amounts
     are marked "auto-verified" and get scraped daily by national.pipeline.
     Others stay "candidate" (review them on the dashboard's Sources view).

Run:  python -m national.discover                 # next batch
      python -m national.discover --state OH      # only Ohio
      python -m national.discover --county 39049  # one county by FIPS
"""
from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote_plus, urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup

from . import config, extract, http, search_providers

log = logging.getLogger("discover")
_lock = threading.Lock()

TAX_WORDS = re.compile(r"\btax (sale|deed|lien|certificate|foreclosure)|delinquent tax|tax collector|"
                       r"treasurer|tax claim|forfeit|tax commissioner|upset sale|repository", re.I)
FORECLOSURE_WORDS = re.compile(r"sheriff|mortgage|foreclosure|judicial sale|court|clerk|master commissioner|"
                               r"commissioner sale|partition", re.I)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def source_id(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:12]


def term_score(text: str) -> int:
    t = " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()) + " "
    if any(f" {n} " in t for n in config.NEGATIVE_TERMS):
        return -10
    s = sum(10 for w in config.STRONG_TERMS if f" {w} " in t)
    s += sum(2 for w in config.WEAK_TERMS if f" {w} " in t)
    return s


def classify_fund_type(context: str) -> str:
    tax = bool(TAX_WORDS.search(context))
    fc = bool(FORECLOSURE_WORDS.search(context))
    if tax and not fc:
        return "tax_sale"
    if fc and not tax:
        return "foreclosure"
    if tax and fc:
        return "tax_sale" if re.search(r"tax (sale|deed)", context, re.I) else "foreclosure"
    return "unknown"


def same_site(host: str, seeds: set[str]) -> bool:
    host = host.lower().removeprefix("www.")
    return any(host == s or host.endswith("." + s) for s in seeds)


def norm_url(u: str) -> str:
    u, _ = urldefrag(u)
    return u.strip()


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


# --------------------------------------------------------------------------
# Per-county discovery
# --------------------------------------------------------------------------

def resolve_seeds(c: dict) -> list[str]:
    seeds = []
    for d in c.get("domains", []):
        seeds.append(d if d.startswith("http") else f"https://{d}/")
    if seeds:
        return seeds
    for d in c.get("guess_domains", []):
        f = http.fetch(f"https://{d}/", check_robots=False, max_bytes=3_000_000, timeout=15)
        if f and f.status == 200 and f.is_html and c["base"].lower() in f.text.lower():
            seeds.append(f.url)
            if len(seeds) >= 2:
                break
    return seeds


def sitemap_urls(base: str, limit: int = 5000) -> list[str]:
    out = []
    for path in ("/sitemap.xml", "/sitemap_index.xml"):
        f = http.fetch(urljoin(base, path), max_bytes=10_000_000, timeout=20)
        if not f or f.status != 200:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", f.text)
        for loc in locs:
            if loc.endswith(".xml") and len(out) < limit:   # nested sitemap
                g = http.fetch(loc, max_bytes=10_000_000, timeout=20)
                if g and g.status == 200:
                    out.extend(re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", g.text)[:limit])
            else:
                out.append(loc)
        if out:
            break
    return out[:limit]


SITE_SEARCH_PATTERNS = [
    "/Search?searchPhrase={q}",       # CivicPlus (very common for counties)
    "/search?q={q}",                  # Granicus OpenCities, many CMSs
    "/?s={q}",                        # WordPress
    "/Search/Results?searchPhrase={q}",
]


def discover_county(c: dict) -> dict:
    """Returns {"county_id", "seeds", "pages", "candidates": [ {url, anchor, found_on, via, score} ]}."""
    seeds = resolve_seeds(c)
    seed_hosts = {urlparse(s).netloc.lower().removeprefix("www.") for s in seeds}
    visited: set[str] = set()
    candidates: dict[str, dict] = {}
    heap: list[tuple[int, int, str, int, str, str]] = []   # (-priority, seq, url, depth, anchor, found_on)
    seq = 0

    def push(url: str, depth: int, anchor: str, found_on: str, prio: int) -> None:
        nonlocal seq
        url = norm_url(url)
        if not url.startswith("http") or url in visited:
            return
        seq += 1
        heapq.heappush(heap, (-prio, seq, url, depth, anchor, found_on))

    def add_candidate(url: str, anchor: str, found_on: str, via: str, score: int) -> None:
        url = norm_url(url)
        prev = candidates.get(url)
        if not prev or score > prev["score"]:
            candidates[url] = {"url": url, "anchor": anchor[:200], "found_on": found_on, "via": via, "score": score}

    for s in seeds:
        push(s, 0, "home", "", 5)
        for u in sitemap_urls(s):
            sc = term_score(u.replace("-", " ").replace("_", " ").replace("/", " "))
            if sc >= 10:
                push(u, 1, "sitemap", s, 50 + sc)
        for pat in SITE_SEARCH_PATTERNS[:2]:
            for q in ("excess funds", "surplus funds"):
                push(urljoin(s, pat.format(q=quote_plus(q))), 1, f"site search: {q}", s, 40)

    pages = 0
    while heap and pages < config.MAX_PAGES_PER_COUNTY:
        negprio, _, url, depth, anchor, found_on = heapq.heappop(heap)
        if url in visited:
            continue
        visited.add(url)
        is_doc = bool(config.DOC_EXT_RE.search(url))
        ctx_score = term_score(f"{anchor} {url.replace('-', ' ').replace('_', ' ')}")
        if is_doc:
            if ctx_score >= 10:
                add_candidate(url, anchor, found_on, "crawl", ctx_score)
            continue
        f = http.fetch(url, max_bytes=8_000_000)
        pages += 1
        if not f or f.status != 200:
            continue
        if not f.is_html:
            if ctx_score >= 10:
                add_candidate(f.url, anchor, found_on, "crawl", ctx_score)
            continue
        soup = BeautifulSoup(f.body, "lxml")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        body_text = soup.get_text(" ", strip=True)[:20000]
        page_score = term_score(f"{title} {anchor}") + min(term_score(body_text[:3000]), 20)
        # A page that itself contains a table/list with amounts is a candidate too
        if page_score >= 12 and (soup.find("table") or extract.MONEY_RE.search(body_text)):
            add_candidate(f.url, title or anchor, found_on, "crawl-page", page_score)
        if depth >= config.MAX_CRAWL_DEPTH:
            continue
        for a in soup.find_all("a", href=True):
            href = urljoin(f.url, a["href"])
            if not href.startswith("http") or href.startswith("mailto"):
                continue
            text = a.get_text(" ", strip=True)
            host = urlparse(href).netloc.lower()
            s = term_score(f"{text} {href.replace('-', ' ').replace('_', ' ')}")
            dept = bool(config.DEPT_LINK_RE.search(text)) or bool(config.DEPT_LINK_RE.search(host))
            onsite = same_site(host, seed_hosts)
            if s < 0:
                continue
            if config.DOC_EXT_RE.search(href):
                if s >= 10:
                    add_candidate(href, text, f.url, "crawl", s)
                continue
            if s >= 10:
                push(href, depth + 1, text, f.url, 30 + s)
            elif dept and (onsite or host.endswith(".gov") or host.endswith(".us")):
                push(href, depth + 1, text, f.url, 20 if not onsite else 15)
                if not onsite:
                    seed_hosts.add(host.removeprefix("www."))   # follow the clerk/treasurer's own site
            elif onsite and depth == 0 and s >= 2:
                push(href, depth + 1, text, f.url, 8)

    return {"county_id": c["id"], "seeds": seeds, "pages": pages, "candidates": list(candidates.values())}


def search_county(c: dict) -> list[dict]:
    out = []
    kind = "Parish" if c["state"] == "LA" else "County"
    name = c["name"] if c["name"].endswith(("County", "Parish", "Borough", "city")) else f"{c['base']} {kind}"
    for q in (f'"{name}" {c["state_name"]} excess funds list',
              f'"{name}" {c["state_name"]} surplus funds tax sale'):
        for url, title, snip in search_providers.search(q):
            if not search_providers.is_official(url, c["base"], c["state"]):
                continue
            s = term_score(f"{title} {snip} {url}")
            if s >= 10:
                out.append({"url": norm_url(url), "anchor": title[:200], "found_on": f"search: {q}",
                            "via": f"search:{search_providers.provider_name()}", "score": s})
    return out


def evaluate_candidate(c: dict, cand: dict) -> Optional[dict]:
    f = http.fetch(cand["url"])
    if not f or f.status != 200 or not f.body:
        return None
    ex = extract.extract(f.body, f.url, f.content_type)
    page_text = ""
    if ex.format == "html":
        page_text = BeautifulSoup(f.body, "lxml").get_text(" ", strip=True)[:4000]
    context = " ".join([cand.get("anchor", ""), cand["url"], cand.get("found_on", ""), page_text])
    good = ex.quality >= config.AUTO_VERIFY_MIN_QUALITY and len(ex.rows) >= config.AUTO_VERIFY_MIN_ROWS
    is_portal = ex.format == "html" and not ex.rows and bool(
        re.search(r"<input[^>]+(type=.?text|type=.?search)", f.text, re.I)) and term_score(page_text) >= 10
    return {
        "id": source_id(f.url),
        "county_id": c["id"], "state": c["state"], "county": c["name"],
        "url": f.url, "anchor": cand.get("anchor", ""), "found_on": cand.get("found_on", ""),
        "via": cand.get("via", ""), "match_score": cand.get("score", 0),
        "format": ex.format, "method": ex.method, "columns": ex.columns,
        "rows": len(ex.rows), "quality": ex.quality, "total": ex.total,
        "fund_type": classify_fund_type(context),
        "status": "auto-verified" if good else ("portal" if is_portal else "candidate"),
        "note": ex.note or ("search form - needs a custom connector" if is_portal else ""),
        "sample": [r.raw for r in ex.rows[:3]],
        "last_checked": now_iso(),
    }


# --------------------------------------------------------------------------
# Batch driver
# --------------------------------------------------------------------------

def pick_batch(counties: list[dict], state_map: dict, args) -> list[dict]:
    if args.county:
        return [c for c in counties if c["id"] in args.county]
    pool = [c for c in counties if not args.state or c["state"] in args.state]
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.RECRAWL_DAYS)

    def last(c):
        ts = state_map.get(c["id"], {}).get("last_crawled")
        return datetime.fromisoformat(ts) if ts else datetime(1970, 1, 1, tzinfo=timezone.utc)

    due = [c for c in pool if last(c) < cutoff]
    due.sort(key=last)
    return due[: args.limit or config.COUNTIES_PER_RUN]


def run(args) -> None:
    counties = load_json(config.COUNTIES_PATH, [])
    if not counties:
        raise SystemExit("data/counties.json missing — run: python -m national.counties")
    state_map = load_json(config.DISCOVERY_STATE_PATH, {})
    sources = {s["id"]: s for s in load_json(config.SOURCES_PATH, [])}
    batch = pick_batch(counties, state_map, args)
    log.info("Discovery batch: %d counties (search provider: %s)", len(batch), search_providers.provider_name())
    search_budget = [config.SEARCH_QUERIES_PER_RUN]

    def work(c: dict) -> tuple[dict, list[dict], dict]:
        res = discover_county(c)
        cands = res["candidates"]
        used_search = False
        if not cands and not args.no_search:
            with _lock:
                ok = search_budget[0] >= 2
                if ok:
                    search_budget[0] -= 2
            if ok:
                cands = search_county(c)
                used_search = True
        cands.sort(key=lambda x: -x["score"])
        evaluated = [e for e in (evaluate_candidate(c, x) for x in cands[:12]) if e]
        st = {"last_crawled": now_iso(), "seeds": res["seeds"], "pages": res["pages"],
              "candidates": len(cands), "verified": sum(1 for e in evaluated if e["status"] == "auto-verified"),
              "searched": used_search}
        return c, evaluated, st

    done = 0
    with ThreadPoolExecutor(max_workers=config.DISCOVERY_WORKERS) as pool:
        futs = [pool.submit(work, c) for c in batch]
        for fut in as_completed(futs):
            try:
                c, evaluated, st = fut.result()
            except Exception as exc:  # noqa: BLE001
                log.error("County failed: %s", exc)
                continue
            with _lock:
                state_map[c["id"]] = st
                for e in evaluated:
                    prev = sources.get(e["id"], {})
                    if prev.get("status") in ("verified", "disabled"):   # manual decisions stick
                        e["status"] = prev["status"]
                    e["first_found"] = prev.get("first_found") or e["last_checked"]
                    sources[e["id"]] = {**prev, **e}
                done += 1
            log.info("[%d/%d] %s, %s: %d pages, %d candidates, %d auto-verified%s",
                     done, len(batch), c["name"], c["state"], st["pages"], st["candidates"],
                     st["verified"], " (via search)" if st["searched"] else "")
            if done % 20 == 0:
                save(state_map, sources)
    save(state_map, sources)
    v = sum(1 for s in sources.values() if s["status"] in ("auto-verified", "verified"))
    log.info("Done. %d sources on file, %d usable; %d/%d counties crawled at least once.",
             len(sources), v, len(state_map), len(counties))


def save(state_map: dict, sources: dict) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.DISCOVERY_STATE_PATH.write_text(json.dumps(state_map, indent=1), encoding="utf-8")
    config.SOURCES_PATH.write_text(
        json.dumps(sorted(sources.values(), key=lambda s: (s["state"], s["county"], -s["rows"])), indent=1),
        encoding="utf-8")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", nargs="*", help="Only these state codes, e.g. OH GA")
    ap.add_argument("--county", nargs="*", help="Only these 5-digit county FIPS codes")
    ap.add_argument("--limit", type=int, default=0, help="Counties this run (default COUNTIES_PER_RUN)")
    ap.add_argument("--no-search", action="store_true", help="Don't use the web-search fallback")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
