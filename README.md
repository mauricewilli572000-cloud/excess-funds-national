# Nationwide Excess Funds Lead Scraper

Finds foreclosure and tax-sale **surplus / excess funds lists** on county
websites across the US on its own, parses whatever format each county uses,
and publishes one scored list with a dashboard and GoHighLevel CSVs.

There's no national database of excess funds. Each county (about 3,150 of them)
publishes its own list, or doesn't publish one at all. So the scraper works in
two stages:

| Stage | Workflow | What it does |
|---|---|---|
| **Discover** | `discover.yml`, every 6 hours | Crawls the next 150 counties' websites looking for excess/surplus lists, test-reads each one it finds, and saves it to `data/sources.json`. Covers the whole country in about 5–6 days, then re-checks each county every 30 days. |
| **Scrape** | `scrape.yml`, daily | Downloads every usable source, parses it, merges it with history, scores it, and publishes the dashboard. |

## Layout

```
national/counties.py        builds data/counties.json (Census county list + CISA .gov domain registry)
national/discover.py        crawler + candidate evaluation
national/search_providers.py web-search fallback (Brave / SerpAPI / Google CSE / DuckDuckGo)
national/extract.py         generic list reader: Excel, CSV, PDF tables, PDF text, web tables, Word
national/pipeline.py        daily scrape -> leads -> history -> scoring -> feeds + CSVs
national/config.py          all knobs (keywords, batch sizes, rate limits)
sources_manual.yaml         your additions, custom connectors, approve/disable overrides
dashboard/index.html        nationwide dashboard (Leads + Sources views)
dashboard/feeds/            index.json, {STATE}.json, top.json, sources.json, *_ghl.csv
data/                       counties, discovery state, sources, history, per-source snapshots
combined-dashboard-update/index.html   your combined dashboard + "Excess Funds (US)" tab
tests/                      extractor tests with sample PDF / HTML / CSV / XLSX lists
```

## How discovery finds lists

1. **Seed sites.** About 2,000 counties have a registered .gov domain (from CISA's
   official registry). The rest get guessed domains like `{name}county{st}.gov` and
   `co.{name}.{st}.us`, which are only used if the homepage actually names the county.
2. **On each site:** `sitemap.xml`, the site's own search box ("excess funds",
   "surplus funds"), then a crawl of up to 60 pages. Links mentioning excess funds,
   surplus, overbid, overage and similar are followed first. Links to the clerk,
   treasurer, sheriff or tax collector are followed next, including onto their
   own separate websites. Things like "surplus property auction", agendas and
   job postings are ignored. The crawler respects `robots.txt` and waits 1 second
   between requests to the same site.
3. **Search fallback.** Counties with nothing on their own sites get two web searches.
   Only official-looking results are kept (.gov, .us, clerk or court domains).
   Known surplus-recovery and "finder" sites are blocked.
4. **Test read.** Each candidate is downloaded and parsed.
   - **auto-verified:** at least 5 rows, and 60% or more have a name or case plus an amount. Scraped daily.
   - **candidate:** found but didn't parse cleanly. Review it on the Sources view.
   - **portal:** a search form instead of a list. It needs a custom connector.

Each source is labeled **Foreclosure** or **Tax sale** from the words around it.

## Web search keys (recommended)

Without a key, the search fallback uses DuckDuckGo, which often blocks automated
traffic, so some counties without a .gov site will be missed. For reliable
coverage, add **one** of these as a repo secret (Settings → Secrets → Actions):

- `BRAVE_API_KEY` (Brave Search API), or
- `SERPAPI_KEY` (SerpAPI), or
- `GOOGLE_CSE_KEY` + `GOOGLE_CSE_CX` (Google Programmable Search)

A full sweep uses at most 2 searches per county that has no list on its own
sites, so up to a few thousand searches a month. `SEARCH_QUERIES_PER_RUN`
(default 200) caps each run. Check the provider's current pricing and free tier.

## Your controls: `sources_manual.yaml`

- **Add a list** discovery missed: `county_id` (5-digit FIPS, shown on the Sources view) + `url`.
- **Plug in a per-county scraper:** `type: json_feed`. Franklin County already
  reads your `franklin-excess-funds` feed (with addresses matched).
  `skip_auto_sources: true` stops the crawler's copy of the same list from double-counting.
- **Approve or turn off** a discovered source: `overrides: {<source id>: {status: verified | disabled}}`.

## Data safety

- A source that suddenly returns nothing, or less than 30% of last time, keeps its
  last good data and shows the error on the Sources view.
- Unchanged files are skipped (compared by content hash).
- A lead is only marked "no longer listed" after its source was actually re-read
  successfully. A download failure never wipes leads.

## Setup

1. Create a public repo `excess-funds-national` and push this folder:
   ```powershell
   git init; git add .; git commit -m "Nationwide excess funds scraper"
   git branch -M main
   git remote add origin https://github.com/mauricewilli572000-cloud/excess-funds-national.git
   git push -u origin main
   ```
2. **Settings → Pages → Source: GitHub Actions.**
3. Optional: add a search API secret (above).
4. **Actions → Discover excess funds sources → Run workflow.** To check results
   quickly, first try `state: OH`, `limit: 20`.
5. **Actions → Scrape excess funds (nationwide) → Run workflow.**
   The dashboard is at `https://mauricewilli572000-cloud.github.io/excess-funds-national/`.
6. Copy `combined-dashboard-update/index.html` over
   `franklin-foreclosure-scraper/dashboard/index.html`. It includes the Franklin
   Excess Funds tab and the new **Excess Funds (US)** tab.

## Running locally

```bash
pip install -r requirements.txt
python -m national.counties                       # seed list (already committed)
python -m national.discover --state OH --limit 10 # discover a few counties
python -m national.pipeline                       # scrape + build feeds
python tests/test_extract.py                      # extractor tests
```

## Limits to expect

- Many counties don't publish a list online at all; you'd need a public-records
  request for those. Others only have a search portal, which needs a custom connector.
- Leads marked **Parsed from text** came from PDFs or pages without a clean
  table. Names and amounts are usually right, but spot-check them.
- National lists rarely include mailing addresses, so skip tracing is still needed.
- States regulate surplus-recovery agreements differently. Some cap fees or
  require waiting periods or registration. Check each state's rules before
  contacting owners about a fee.
