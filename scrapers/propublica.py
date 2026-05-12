"""ProPublica Nonprofits API scraper — 5th source (supplemental).

Three-track strategy for sustainability-aligned results:
  Track 1: Broad beauty terms restricted to NTEE=C (Environment sector) via API param
  Track 2: Known beauty brand names, kept only if org name contains a sustainability keyword
  Track 3: Beauty-specific queries (no NTEE restriction), kept if org name contains a beauty keyword

API docs: https://projects.propublica.org/nonprofits/api/
"""

import random
import re
import sys
import time
from datetime import datetime, timezone

import requests

BASE_URL = "https://projects.propublica.org/nonprofits/api/v2/search.json"

# Track 1: beauty/cosmetics terms restricted to NTEE=C (Environment sector)
# ProPublica returns results for single-word queries; multi-word queries often return 0
SUSTAINABILITY_QUERIES = [
    "cosmetics",
    "skincare",
    "beauty",
    "salon",
    "personal care",
    "body care",
]

# Track 2: known beauty brand foundations — bypassed by EIN whitelist or
# kept only if name contains a sustainability keyword
BRAND_QUERIES = [
    "loreal foundation",
    "estee lauder foundation",
    "aveda",
    "revlon foundation",
    "shiseido foundation",
    "body shop",
    "dove foundation",
    "neutrogena",
    "clinique",
    "maybelline",
    "origins foundation",
    "kiehls",
]

# Track 3: broad beauty queries (no NTEE restriction) — kept only if org name
# contains a beauty keyword. Captures beauty orgs outside NTEE=C.
BEAUTY_QUERIES = [
    "cosmetics foundation",
    "beauty foundation",
    "skincare foundation",
    "haircare foundation",
    "wellness beauty",
    "botanical beauty",
    "aromatherapy",
    "holistic beauty",
]

# Known EINs for major beauty brand foundations — bypass name filter for these
KNOWN_BEAUTY_EINS = {
    13_3566561,   # Estee Lauder Charitable Foundation
    26_1281977,   # L'Oréal USA for Women in Science
    22_3728131,   # Loreal Family Foundation (example)
    20_4286033,   # Shiseido Americas Foundation
    47_1234567,   # The Body Shop Foundation (placeholder — verify EIN)
}

# Keywords indicating sustainability alignment in an org's name
SUSTAINABILITY_NAME_KEYWORDS = (
    "green", "sustain", "environ", "eco", "natural", "clean",
    "organic", "climate", "earth", "conserv", "renew",
)

# Keywords that confirm an org is cosmetics/beauty-product related (for Track 1 & 3 post-filter)
COSMETICS_NAME_KEYWORDS = (
    "cosmetic", "beauty", "skincare", "skin care", "personal care",
    "makeup", "haircare", "hair care", "fragrance", "perfume",
    "salon", "spa", "botanical", "derma", "nail", "body care",
    "wellness", "aromatherapy", "loreal", "estee", "aveda", "revlon",
    "shiseido", "clinique", "origins", "kiehl", "neutrogena", "dove",
    "maybelline", "lancome", "nars", "bobbi brown",
)

NTEE_SECTOR_MAP = {
    "C": "Environment",
    "C20": "Pollution Abatement & Control",
    "C30": "Natural Resources Conservation",
    "C40": "Water/Air/Waste Management",
    "T": "Philanthropy & Grantmaking",
    "E": "Health",
}

MAX_RETRIES = 3
REQUEST_TIMEOUT = 30  # seconds


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fetch_officers(ein: int) -> list[dict]:
    """Scrape officer names and titles from the ProPublica Nonprofit Explorer page."""
    try:
        time.sleep(random.uniform(1.5, 3))
        resp = requests.get(
            f"https://projects.propublica.org/nonprofits/organizations/{ein}",
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        section = re.search(
            r"Key Employees and Officers(.*?)(?:Financial Statements|Fiscal Year|Form 990 documents)",
            resp.text,
            re.DOTALL,
        )
        if not section:
            return []
        rows = re.findall(r"<td[^>]*>(.*?)</td>", section.group(1), re.DOTALL)
        officers = []
        for row in rows:
            clean = re.sub(r"<[^>]+>", " ", row).strip()
            clean = re.sub(r"&amp;", "&", clean)
            clean = re.sub(r"&#39;", "'", clean)
            clean = re.sub(r"\s+", " ", clean).strip()
            match = re.match(r"^(.+?)\s*\((.+?)\)\s*$", clean)
            if match:
                name = match.group(1).strip()
                title = match.group(2).strip()
                if name and title and not name[0].isdigit() and "$" not in name:
                    officers.append({"name": name, "title": title})
        return officers
    except Exception as exc:
        print(f"[propublica] Could not fetch officers for EIN {ein}: {exc}", file=sys.stderr)
        return []


def _get_with_retry(url: str, params: dict) -> "dict | None":
    """GET request with exponential backoff retries. Returns parsed JSON or None."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 404:
                # API returns 404 when query has zero results — not a retriable error
                return {"organizations": [], "num_pages": 0}
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


def _fetch_all_pages(query: str, ntee: str = None) -> list[dict]:
    """Fetch all pages for a single query. Pass ntee='C' to restrict to Environment sector."""
    results: list[dict] = []
    page = 0

    while True:
        delay = random.uniform(2, 5)
        time.sleep(delay)

        params = {"q": query, "page": page}
        if ntee:
            params["selected_ntee"] = ntee

        data = _get_with_retry(BASE_URL, params)
        if data is None:
            print(f"[propublica] Failed to fetch page {page} for query '{query}' — stopping", file=sys.stderr)
            break

        orgs = data.get("organizations", [])
        if not orgs:
            break

        results.extend(orgs)

        num_pages = data.get("num_pages", 1)
        if page >= num_pages - 1:
            break

        print(f"[propublica] '{query}' page {page + 1}/{num_pages} — {len(orgs)} orgs", file=sys.stderr)
        page += 1

    return results


def _has_sustainability_name(org: dict) -> bool:
    """Return True if the org name contains a sustainability-related keyword."""
    name = (org.get("name") or "").lower()
    return any(kw in name for kw in SUSTAINABILITY_NAME_KEYWORDS)


def _is_cosmetics_org(org: dict) -> bool:
    """Return True if the org name contains a cosmetics/beauty-product keyword."""
    name = (org.get("name") or "").lower()
    return any(kw in name for kw in COSMETICS_NAME_KEYWORDS)


def _is_known_beauty_brand(org: dict) -> bool:
    """Return True if the org's EIN is in the known beauty brand whitelist."""
    return org.get("ein") in KNOWN_BEAUTY_EINS


def _sector_label(ntee_code: "str | None") -> str:
    if not ntee_code:
        return ""
    # Try full code first (e.g. "C30"), then prefix (e.g. "C")
    return NTEE_SECTOR_MAP.get(ntee_code, NTEE_SECTOR_MAP.get(ntee_code[0].upper(), ntee_code))


def _map_org(org: dict, officers: list[dict] = None) -> dict:
    """Map a ProPublica org dict to the unified schema."""
    ntee = org.get("ntee_code") or ""
    ein = org.get("strein") or str(org.get("ein", ""))
    name_lower = (org.get("name") or "").lower()

    # Extract actual sustainability keywords found in the org name
    kw_matches = [kw for kw in SUSTAINABILITY_NAME_KEYWORDS if kw in name_lower]
    sustainability_keywords = ["nonprofit"] + kw_matches + ([ntee] if ntee else [])

    # Format officers as "Name (Title); Name (Title)" for the main sheet column
    officers_str = "; ".join(
        f"{o['name']} ({o['title']})" for o in (officers or [])
    )

    return {
        "company_name": org.get("name") or org.get("sub_name") or "",
        "source": "propublica",
        "disclosure_status": bool(org.get("have_filings")),
        "score_or_rating": str(round(org.get("score", 0), 2)),
        "sector": _sector_label(ntee),
        "year_of_disclosure": None,
        "report_url": f"https://projects.propublica.org/nonprofits/organizations/{org.get('ein', '')}",
        "funding_type": "grant",
        "beauty_alignment": True,
        "sustainability_keywords": sustainability_keywords,
        "scraped_at": _now_utc(),
        "notes": f"EIN: {ein} | City: {org.get('city', '')} {org.get('state', '')}",
        "officers": officers_str,
        # Raw list kept for the Officers tab (not written to main schema columns)
        "_officers_raw": officers or [],
        "_ein_int": org.get("ein"),
    }


def run() -> list[dict]:
    """Entry point. Returns sustainability-filtered unified schema records."""
    seen_eins: set = set()
    records: list[dict] = []

    # Track 1: NTEE=C (Environment) + cosmetics name check — both required
    for query in SUSTAINABILITY_QUERIES:
        print(f"[propublica] Track 1 (NTEE=C + cosmetics filter): '{query}'", file=sys.stderr)
        raw = _fetch_all_pages(query, ntee="C")
        if not raw:
            print(f"[propublica] WARNING: 0 results for '{query}'", file=sys.stderr)
            continue
        filtered = [org for org in raw if _is_cosmetics_org(org)]
        print(
            f"[propublica] '{query}': {len(raw)} env orgs → {len(filtered)} after cosmetics name filter",
            file=sys.stderr,
        )
        for org in filtered:
            ein = org.get("ein")
            if ein in seen_eins:
                continue
            seen_eins.add(ein)
            officers = _fetch_officers(ein)
            records.append(_map_org(org, officers))

    # Track 2: Brand queries — keep if sustainability keyword in name OR known EIN
    for query in BRAND_QUERIES:
        print(f"[propublica] Track 2 (brand + sustainability/EIN filter): '{query}'", file=sys.stderr)
        raw = _fetch_all_pages(query)
        if not raw:
            print(f"[propublica] WARNING: 0 results for '{query}'", file=sys.stderr)
            continue
        filtered = [
            org for org in raw
            if _has_sustainability_name(org) or _is_known_beauty_brand(org)
        ]
        print(
            f"[propublica] '{query}': {len(raw)} raw → {len(filtered)} after filter",
            file=sys.stderr,
        )
        for org in filtered:
            ein = org.get("ein")
            if ein in seen_eins:
                continue
            seen_eins.add(ein)
            officers = _fetch_officers(ein)
            records.append(_map_org(org, officers))

    # Track 3: Beauty-specific queries (no NTEE restriction) — keep if beauty keyword in name
    for query in BEAUTY_QUERIES:
        print(f"[propublica] Track 3 (beauty queries, no NTEE restriction): '{query}'", file=sys.stderr)
        raw = _fetch_all_pages(query)
        if not raw:
            print(f"[propublica] WARNING: 0 results for '{query}'", file=sys.stderr)
            continue
        filtered = [org for org in raw if _is_cosmetics_org(org)]
        print(
            f"[propublica] '{query}': {len(raw)} raw → {len(filtered)} after beauty name filter",
            file=sys.stderr,
        )
        for org in filtered:
            ein = org.get("ein")
            if ein in seen_eins:
                continue
            seen_eins.add(ein)
            officers = _fetch_officers(ein)
            records.append(_map_org(org, officers))

    print(f"[propublica] Total: {len(records)} unique sustainability-aligned records", file=sys.stderr)

    if not records:
        print("[propublica] WARNING: 0 records — skipping Sheets sync", file=sys.stderr)

    return records
