"""Build data/counties.json — every US county (50 states + DC) with its likely
official website domains.

Seeds:
  * County list: the `addfips` package (Census 2020 county FIPS file).
  * Official domains: CISA's registry of every .gov domain
    (github.com/cisagov/dotgov-data, current-full.csv), filtered to domain
    type "County" and matched to a county by organization name + state.
  * Guessed domains for counties without a .gov match (franklincountyoh.gov,
    co.franklin.oh.us, ...). Guesses are only kept if the homepage loads and
    mentions the county name — that check happens during discovery.

Run:  python -m national.counties
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from collections import defaultdict

import addfips
import requests

from . import config

log = logging.getLogger(__name__)

DOTGOV_URL = "https://raw.githubusercontent.com/cisagov/dotgov-data/main/current-full.csv"
TERRITORIES = {"PR", "GU", "VI", "AS", "MP", "UM"}
SUFFIXES = ("County", "Parish", "Borough", "Census Area", "City and Borough", "Municipality",
            "city", "City", "Planning Region")


def base_name(name: str) -> str:
    n = name
    for s in sorted(SUFFIXES, key=len, reverse=True):
        if n.endswith(" " + s):
            n = n[: -len(s) - 1]
            break
    return n.strip()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower().replace("saint ", "st").replace("st. ", "st"))


def load_census_counties() -> list[dict]:
    data_dir = os.path.join(os.path.dirname(addfips.__file__), "data")
    states = {r["fips"]: r for r in csv.DictReader(open(os.path.join(data_dir, "states.csv"), encoding="utf-8"))}
    out = []
    for r in csv.DictReader(open(os.path.join(data_dir, "counties_2020.csv"), encoding="utf-8")):
        st = states.get(r["statefp"])
        if not st or st["postal"] in TERRITORIES:
            continue
        out.append({
            "id": r["statefp"] + r["countyfp"],
            "state": st["postal"],
            "state_name": st["name"],
            "name": r["name"],
            "base": base_name(r["name"]),
        })
    return out


def load_dotgov() -> list[dict]:
    r = requests.get(DOTGOV_URL, timeout=60)
    r.raise_for_status()
    return list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))


PREFERRED_DOMAIN_RE = re.compile(r"clerk|court|treas|sheriff|tax|auditor|fiscal|recorder|finance|trustee|collector")
NOISE_DOMAIN_RE = re.compile(r"health|librar|boe|elect|vote|jfs|parks|911|ems|fire|water|sewer|transit|airport|"
                             r"tourism|visit|school|museum|animal|dd\b|mental|jobs|workforce|library")


def domain_rank(d: str) -> int:
    return 0 if PREFERRED_DOMAIN_RE.search(d) else 1


def guess_domains(c: dict) -> list[str]:
    b, st = slug(c["base"]), c["state"].lower()
    kind = "parish" if c["state"] == "LA" else "county"
    return [
        f"{b}{kind}{st}.gov", f"{b}{kind}.gov", f"{b}co{st}.gov", f"co.{b}.{st}.us",
        f"{b}{kind}{st}.org", f"{b}{kind}.org", f"{b}{kind}{st}.com", f"{b}{kind}.us",
        f"www.co.{b}.{st}.us", f"{b}{kind}-{st}.gov",
    ]


def build() -> list[dict]:
    counties = load_census_counties()
    try:
        dotgov = load_dotgov()
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not load .gov registry (%s); using guessed domains only", exc)
        dotgov = []

    by_state: dict[str, list[dict]] = defaultdict(list)
    for row in dotgov:
        if row.get("Domain type") in ("County", "City") or "county" in row.get("Organization name", "").lower():
            by_state[row.get("State", "")].append(row)

    for c in counties:
        exact, related = [], []
        names = {c["name"].lower(), f"{c['base']} county".lower(), f"{c['base']} parish".lower(),
                 f"county of {c['base']}".lower(), f"{c['base']} borough".lower()}
        if c["name"].endswith("city"):          # VA / MD / MO / NV independent cities
            names |= {f"city of {c['base']}".lower(), c["base"].lower()}
        for row in by_state.get(c["state"], []):
            org = row["Organization name"].strip().lower()
            dom = row["Domain name"].strip().lower()
            if org in names or org == f"city and county of {c['base']}".lower():
                exact.append(dom)
            elif any(org.startswith(n + " ") for n in names):
                related.append(dom)   # "Cuyahoga County Prosecutor's Office" etc.
        related.sort(key=domain_rank)
        c["domains"] = [d for d in dict.fromkeys(exact + related) if not NOISE_DOMAIN_RE.search(d)][:8]
        c["guess_domains"] = [d for d in guess_domains(c) if d not in c["domains"]]
    have = sum(1 for c in counties if c["domains"])
    log.info("%d counties; %d have at least one registered .gov domain", len(counties), have)
    return counties


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    counties = build()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.COUNTIES_PATH.write_text(json.dumps(counties, indent=1), encoding="utf-8")
    log.info("Wrote %s", config.COUNTIES_PATH)


if __name__ == "__main__":
    main()
