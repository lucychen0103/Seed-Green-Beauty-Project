"""ProPublica Nonprofit Explorer scraper — foundation grant history leads.

ProPublica Nonprofit Explorer is an IRS 990 database, NOT a grant listings
engine. This scraper searches for foundations operating in sustainability,
beauty, and environment spaces and extracts their grant history (recipient
organisations, amounts, years) as leads for the team to target directly.
"""

import asyncio
import logging
from typing import Any, Dict, List

import requests

from scrapers.base import FundingRecord
from scrapers.utils import random_delay, retry_async, tag_record

logger = logging.getLogger(__name__)

SEARCH_URL = "https://projects.propublica.org/nonprofits/api/v2/search.json"
ORG_URL = "https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json"
PROFILE_URL = "https://projects.propublica.org/nonprofits/organizations/{ein}"

# Foundation search terms — targets foundations likely to fund
# sustainability, clean beauty, and environmental CPG initiatives.
KEYWORDS = [
    "sustainable beauty foundation",
    "environmental beauty",
    "clean cosmetics fund",
    "sustainability foundation",
    "green consumer goods",
]


async def scrape() -> List[FundingRecord]:
    """Return foundation leads from ProPublica Nonprofit Explorer."""
    records: List[FundingRecord] = []
    seen_eins: set = set()

    for keyword in KEYWORDS:
        try:
            kw_records = await _scrape_keyword(keyword, seen_eins)
            records.extend(kw_records)
        except Exception as exc:
            logger.error(
                "propublica: keyword %r failed: %s", keyword, exc, exc_info=True
            )

    if not records:
        logger.warning("propublica: scraper returned 0 results")

    return records


async def _scrape_keyword(keyword: str, seen_eins: set) -> List[FundingRecord]:
    records: List[FundingRecord] = []
    page = 0

    while True:
        params = {"q": keyword, "page": page}

        def _get(p: Dict = params) -> Dict:
            resp = requests.get(SEARCH_URL, params=p, timeout=30)
            resp.raise_for_status()
            return resp.json()

        data = await retry_async(lambda: asyncio.to_thread(_get))
        orgs: List[Dict[str, Any]] = data.get("organizations", [])

        if not orgs:
            break

        for org in orgs:
            ein = str(org.get("ein") or "")
            if not ein or ein in seen_eins:
                continue
            seen_eins.add(ein)

            try:
                record = await _build_record(org, ein)
                tag_record(record, f"{record.company_name} {record.notes}")
                records.append(record)
            except Exception as exc:
                logger.warning("propublica: failed to build record for EIN %s: %s", ein, exc)

        # ProPublica returns up to 100 results; stop if fewer than a full page
        if len(orgs) < 100:
            break

        page += 1
        await random_delay()

    return records


async def _build_record(org: Dict[str, Any], ein: str) -> FundingRecord:
    """Fetch organisation detail (grant history) and build a FundingRecord."""

    def _get_detail() -> Dict:
        resp = requests.get(ORG_URL.format(ein=ein), timeout=30)
        resp.raise_for_status()
        return resp.json()

    detail = await retry_async(lambda: asyncio.to_thread(_get_detail))
    org_detail: Dict[str, Any] = detail.get("organization", {})
    filings: List[Dict[str, Any]] = detail.get("filings_with_data", [])

    # Summarise grant history from the most recent filings
    grant_lines: List[str] = []
    for filing in filings[:3]:  # limit to 3 most recent years
        year = filing.get("tax_prd_yr", "")
        total_grants = filing.get("totgrants", "")
        revenue = filing.get("totrevenue", "")
        if year:
            grant_lines.append(
                f"Year:{year} totalGrantsAwarded:{total_grants} revenue:{revenue}"
            )

    notes = " | ".join(grant_lines) if grant_lines else "No filing data available"

    ntee = org_detail.get("ntee_code", "")
    state = org_detail.get("state", "")
    city = org_detail.get("city", "")
    latest_year = filings[0].get("tax_prd_yr") if filings else None

    return FundingRecord(
        company_name=org.get("name", org_detail.get("name", "")),
        source="propublica",
        source_track="Grants & Funding",
        disclosure_status=True,
        sector=f"{ntee} — {city}, {state}".strip(" —"),
        year_of_disclosure=int(latest_year) if latest_year else None,
        report_url=PROFILE_URL.format(ein=ein),
        funding_type="grant",
        is_open=None,
        notes=notes,
    )
