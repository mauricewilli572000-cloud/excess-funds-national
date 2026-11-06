"""Web search fallback for counties whose own sites didn't turn up a list.

Providers (first one with credentials wins):
  * Brave Search API   — env BRAVE_API_KEY
  * SerpAPI (Google)   — env SERPAPI_KEY
  * Google Programmable Search — env GOOGLE_CSE_KEY + GOOGLE_CSE_CX
  * DuckDuckGo HTML    — no key; best-effort, often rate-limited/blocked
Each returns a list of (url, title, snippet).
"""
from __future__ import annotations

import logging
import time
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from . import config, http

log = logging.getLogger(__name__)


def _brave(q: str) -> list[tuple[str, str, str]]:
    r = http.session().get("https://api.search.brave.com/res/v1/web/search",
                           params={"q": q, "count": 10, "country": "us"},
                           headers={"X-Subscription-Token": config.BRAVE_API_KEY, "Accept": "application/json"},
                           timeout=30)
    r.raise_for_status()
    return [(x.get("url", ""), x.get("title", ""), x.get("description", ""))
            for x in r.json().get("web", {}).get("results", [])]


def _serpapi(q: str) -> list[tuple[str, str, str]]:
    r = http.session().get("https://serpapi.com/search.json",
                           params={"engine": "google", "q": q, "api_key": config.SERPAPI_KEY, "num": 10},
                           timeout=60)
    r.raise_for_status()
    return [(x.get("link", ""), x.get("title", ""), x.get("snippet", ""))
            for x in r.json().get("organic_results", [])]


def _google_cse(q: str) -> list[tuple[str, str, str]]:
    r = http.session().get("https://www.googleapis.com/customsearch/v1",
                           params={"key": config.GOOGLE_CSE_KEY, "cx": config.GOOGLE_CSE_CX, "q": q, "num": 10},
                           timeout=30)
    r.raise_for_status()
    return [(x.get("link", ""), x.get("title", ""), x.get("snippet", "")) for x in r.json().get("items", [])]


def _ddg(q: str) -> list[tuple[str, str, str]]:
    time.sleep(4)  # be very gentle — this endpoint blocks fast clients
    r = http.session().post("https://html.duckduckgo.com/html/", data={"q": q}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"DuckDuckGo returned {r.status_code}")
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for a in soup.select("a.result__a"):
        href = a.get("href", "")
        if "uddg=" in href:
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
        snip = a.find_parent(class_="result")
        out.append((href, a.get_text(" ", strip=True),
                    snip.get_text(" ", strip=True)[:300] if snip else ""))
    return out


def provider_name() -> str:
    if config.BRAVE_API_KEY:
        return "brave"
    if config.SERPAPI_KEY:
        return "serpapi"
    if config.GOOGLE_CSE_KEY and config.GOOGLE_CSE_CX:
        return "google_cse"
    return "duckduckgo"


_DISABLED = {"duckduckgo": False}


def search(q: str) -> list[tuple[str, str, str]]:
    name = provider_name()
    if _DISABLED.get(name):
        return []
    fn = {"brave": _brave, "serpapi": _serpapi, "google_cse": _google_cse, "duckduckgo": _ddg}[name]
    try:
        return fn(q)
    except Exception as exc:  # noqa: BLE001
        log.warning("Search (%s) failed for %r: %s", name, q, exc)
        if name == "duckduckgo":
            _DISABLED[name] = True   # blocked: stop trying for the rest of this run
            log.warning("DuckDuckGo fallback disabled for this run. Add BRAVE_API_KEY / SERPAPI_KEY "
                        "secrets for reliable search.")
        return []


def is_official(url: str, county_base: str, state: str) -> bool:
    host = urlparse(url).netloc.lower()
    if not host or any(b in host for b in config.SEARCH_BLOCKLIST):
        return False
    slug = "".join(ch for ch in county_base.lower() if ch.isalnum())
    st = state.lower()
    return (host.endswith(".gov") or host.endswith(f".{st}.us") or host.endswith(".us")
            or slug in host.replace("-", "") or "clerk" in host or "court" in host or "treasurer" in host)
