"""Shared settings for the nationwide excess funds scraper."""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DASHBOARD_DIR = ROOT / "dashboard"
FEEDS_DIR = DASHBOARD_DIR / "feeds"
CACHE_DIR = ROOT / ".cache"

COUNTIES_PATH = DATA_DIR / "counties.json"            # seed list (built by national.counties)
DISCOVERY_STATE_PATH = DATA_DIR / "discovery_state.json"  # per-county crawl status
SOURCES_PATH = DATA_DIR / "sources.json"              # every candidate source found
MANUAL_SOURCES_PATH = ROOT / "sources_manual.yaml"    # your overrides / hand-added sources
HISTORY_PATH = DATA_DIR / "history.json"              # every lead ever seen
SOURCE_SNAPSHOTS_DIR = DATA_DIR / "source_leads"      # last good extraction per source

USER_AGENT = os.environ.get(
    "SCRAPER_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
)
REQUEST_TIMEOUT = 30
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024
PER_HOST_DELAY = float(os.environ.get("PER_HOST_DELAY", "1.0"))   # seconds between hits to one host
RESPECT_ROBOTS = os.environ.get("RESPECT_ROBOTS", "1") != "0"

# --- Discovery -------------------------------------------------------------
COUNTIES_PER_RUN = int(os.environ.get("COUNTIES_PER_RUN", "150"))
RECRAWL_DAYS = int(os.environ.get("RECRAWL_DAYS", "30"))
MAX_PAGES_PER_COUNTY = int(os.environ.get("MAX_PAGES_PER_COUNTY", "60"))
MAX_CRAWL_DEPTH = int(os.environ.get("MAX_CRAWL_DEPTH", "3"))
DISCOVERY_WORKERS = int(os.environ.get("DISCOVERY_WORKERS", "8"))
SEARCH_QUERIES_PER_RUN = int(os.environ.get("SEARCH_QUERIES_PER_RUN", "200"))

# Words that mark a page/link as an excess-funds list (scored), and words
# that mean it's something else that happens to say "surplus".
STRONG_TERMS = [
    "excess funds", "excess proceeds", "excess sale proceeds", "excess sales proceeds",
    "surplus funds", "surplus proceeds", "overbid", "over bid", "overage",
    "excess tax sale", "tax sale excess", "tax sale surplus", "tax deed surplus",
    "surplus from tax sale", "unclaimed surplus", "unclaimed excess", "excess bid",
    "sheriff sale surplus", "foreclosure surplus", "unapplied funds", "unapplied excess",
]
WEAK_TERMS = ["unclaimed funds", "unclaimed money", "surplus", "excess", "tax deed", "tax sale",
              "sheriff sale", "foreclosure"]
NEGATIVE_TERMS = [
    "surplus property", "surplus equipment", "surplus vehicle", "surplus auction", "surplus items",
    "budget surplus", "excess property", "govdeals", "publicsurplus", "excess liability",
    "excess baggage", "excess heat", "flood", "job", "employment", "rfp", "bid opportunities",
    "minutes", "agenda", "court costs", "filing fees", "fee schedule", "cost deposit",
]
# Departments worth following off the main county site (clerk, treasurer...).
DEPT_LINK_RE = re.compile(
    r"clerk|treasurer|tax collector|sheriff|court|trustee|prothonotary|recorder|"
    r"finance|revenue|tax commissioner|tax claim|auditor|comptroller",
    re.I,
)
DOC_EXT_RE = re.compile(r"\.(pdf|xlsx?|csv|txt|docx?)(?:$|\?)", re.I)

# --- Search fallback --------------------------------------------------------
# Set ONE of these as GitHub Actions secrets. Without any, DuckDuckGo's HTML
# endpoint is tried at a low rate (it often blocks bots — treat as best-effort).
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
GOOGLE_CSE_KEY = os.environ.get("GOOGLE_CSE_KEY", "")
GOOGLE_CSE_CX = os.environ.get("GOOGLE_CSE_CX", "")
# Result domains that are never the county itself (finders, aggregators).
SEARCH_BLOCKLIST = [
    "facebook.com", "youtube.com", "reddit.com", "linkedin.com", "twitter.com", "x.com",
    "yelp.com", "zillow.com", "realtor.com", "redfin.com", "trulia.com", "wikipedia.org",
    "surplus", "excessfund", "overage", "fundsfinder", "assetrecovery", "biggerpockets",
    "tiktok.com", "instagram.com", "pinterest.com", "amazon.com", "scribd.com",
]

# --- Source verification ---------------------------------------------------
AUTO_VERIFY_MIN_ROWS = 5         # extraction must find at least this many leads with amounts
AUTO_VERIFY_MIN_QUALITY = 0.6    # share of rows that have a name AND an amount
