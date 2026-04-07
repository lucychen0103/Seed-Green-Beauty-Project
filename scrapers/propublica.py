"""ProPublica Nonprofits API scraper — 5th source (supplemental).

Queries the ProPublica Nonprofit Explorer API for beauty/cosmetics-related
nonprofit organisations, filters by NTEE code, deduplicates by EIN, and
returns records conforming to the unified schema.

API docs: https://projects.propublica.org/nonprofits/api/
"""

import random
import sys
import time
from datetime import datetime, timezone

import requests

BASE_URL = "https://projects.propublica.org/nonprofits/api/v2/search.json"

# Targeted queries — specific enough to reduce noise
QUERIES = [
    "loreal foundation",
    "estee lauder foundation",
    "aveda",
    "cosmetics sustainability",
    "beauty environmental foundation",
]

# Keep only Philanthropy/Foundations (T), Environment (C), and Health (E) nonprofits
ALLOWED_NTEE_PREFIXES = ("T", "C", "E")

# Human-readable sector labels for the three allowed NTEE prefixes
NTEE_SECTOR_MAP = {
    "T": "Philanthropy, Voluntarism & Grantmaking",
    "C": "Environment",
    "E": "Health — General & Rehabilitative",
}

MAX_RETRIES = 3
REQUEST_TIMEOUT = 30  # seconds


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_with_retry(url: str, params: dict) -> dict | None:
    """GET request with exponential backoff retries. Returns parsed JSON or None."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as exc:
            print(f"[propublica] HTTP error on attempt {attempt + 1}: {exc}", file=sys.stderr)
        except requests.exceptions.RequestException as exc:
            print(f"[propublica] Request error on attempt {attempt + 1}: {exc}", file=sys.stderr)

        if attempt < MAX_RETRIES - 1:
            sleep_secs = 2 ** (attempt + 1)
            print(f"[propublica] Retrying in {sleep_secs}s...", file=sys.stderr)
            time.sleep(sleep_secs)

    return None


def _fetch_all_pages(query: str) -> list[dict]:
    """Fetch all pages for a single query. Returns raw org dicts from API."""
    results: list[dict] = []
    page = 0

    while True:
        delay = random.uniform(2, 5)
        time.sleep(delay)

        data = _get_with_retry(BASE_URL, {"q": query, "page": page})
        if data is None:
            print(f"[propublica] Failed to fetch page {page} for query '{query}' — stopping pagination", file=sys.stderr)
            break

        orgs = data.get("organizations", [])
        if not orgs:
            break

        results.extend(orgs)

        num_pages = data.get("num_pages", 1)
        if page >= num_pages - 1:
            break

        print(f"[propublica] Query '{query}' page {page + 1}/{num_pages} — {len(orgs)} orgs", file=sys.stderr)
        page += 1

    return results


def _is_allowed_ntee(ntee_code: str | None) -> bool:
    if not ntee_code:
        return False
    return ntee_code.upper().startswith(ALLOWED_NTEE_PREFIXES)


def _sector_label(ntee_code: str | None) -> str:
    if not ntee_code:
        return ""
    prefix = ntee_code[0].upper()
    return NTEE_SECTOR_MAP.get(prefix, ntee_code)


def _map_org(org: dict) -> dict:
    """Map a ProPublica org dict to the unified schema."""
    ntee = org.get("ntee_code") or ""
    ein = org.get("strein") or str(org.get("ein", ""))
    return {
        "company_name": org.get("name") or org.get("sub_name") or "",
        "source": "propublica",
        "disclosure_status": bool(org.get("have_filings")),
        "score_or_rating": str(round(org.get("score", 0), 2)),
        "sector": _sector_label(ntee),
        "year_of_disclosure": None,  # not available from search endpoint
        "report_url": f"https://projects.propublica.org/nonprofits/organizations/{org.get('ein', '')}",
        "funding_type": "grant",
        "beauty_alignment": True,
        "sustainability_keywords": ["nonprofit", ntee] if ntee else ["nonprofit"],
        "scraped_at": _now_utc(),
        "notes": f"EIN: {ein} | City: {org.get('city', '')} {org.get('state', '')}",
    }


def run() -> list[dict]:
    """Entry point. Returns unified schema records from ProPublica nonprofit search."""
    seen_eins: set[int] = set()
    records: list[dict] = []

    for query in QUERIES:
        print(f"[propublica] Querying: '{query}'", file=sys.stderr)
        raw = _fetch_all_pages(query)

        if not raw:
            print(f"[propublica] WARNING: 0 results for query '{query}'", file=sys.stderr)
            continue

        filtered = [org for org in raw if _is_allowed_ntee(org.get("ntee_code"))]
        print(
            f"[propublica] '{query}': {len(raw)} raw → {len(filtered)} after NTEE filter",
            file=sys.stderr,
        )

        for org in filtered:
            ein = org.get("ein")
            if ein in seen_eins:
                continue
            seen_eins.add(ein)
            records.append(_map_org(org))

    print(f"[propublica] Total: {len(records)} unique records", file=sys.stderr)

    if not records:
        print("[propublica] WARNING: 0 records returned — skipping Sheets sync for this source", file=sys.stderr)

    return records
