"""Grants.gov REST API scraper — government funding opportunities.

Searches open opportunities by sustainability/beauty keywords using the
Grants.gov v2 JSON API (no auth required).
"""

import asyncio
import logging
from typing import Any, Dict, List, Set

import requests

from scrapers.base import FundingRecord
from scrapers.utils import random_delay, retry_async, tag_record

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.grants.gov/v2/opportunities/search"
DETAIL_URL = "https://www.grants.gov/search-grants/opportunity/detail?oppId={}"
PAGE_SIZE = 25

# Keywords searched sequentially; deduplication by opportunityId across all passes.
KEYWORDS = [
    "sustainable beauty",
    "clean beauty",
    "green cosmetics",
    "sustainability personal care",
    "environmental consumer packaged goods",
]


async def scrape() -> List[FundingRecord]:
    """Return open Grants.gov opportunities matching beauty/sustainability keywords."""
    records: List[FundingRecord] = []
    seen_ids: Set[str] = set()

    for keyword in KEYWORDS:
        try:
            kw_records = await _scrape_keyword(keyword, seen_ids)
            records.extend(kw_records)
        except Exception as exc:
            logger.error(
                "grants_gov: keyword %r failed: %s", keyword, exc, exc_info=True
            )

    if not records:
        logger.warning("grants_gov: scraper returned 0 results")

    return records


async def _scrape_keyword(keyword: str, seen_ids: Set[str]) -> List[FundingRecord]:
    records: List[FundingRecord] = []
    offset = 0

    while True:
        payload = {
            "keyword": keyword,
            "oppStatuses": "posted",
            "rows": PAGE_SIZE,
            "startRecordNum": offset,
        }

        def _post(p: Dict = payload) -> Dict:
            resp = requests.post(SEARCH_URL, json=p, timeout=30)
            resp.raise_for_status()
            return resp.json()

        data = await retry_async(lambda: asyncio.to_thread(_post))

        hits: List[Dict[str, Any]] = data.get("data", {}).get("oppHits", [])
        if not hits:
            break

        for hit in hits:
            opp_id = str(hit.get("id") or hit.get("opportunityId") or "")
            if not opp_id or opp_id in seen_ids:
                continue
            seen_ids.add(opp_id)
            record = _build_record(hit, opp_id)
            tag_record(record, f"{record.company_name} {record.notes}")
            records.append(record)

        total: int = data.get("data", {}).get("hitCount", 0)
        offset += PAGE_SIZE
        if offset >= total:
            break

        await random_delay()

    return records


def _build_record(hit: Dict[str, Any], opp_id: str) -> FundingRecord:
    synopsis = hit.get("synopsis") or {}
    description = synopsis.get("synopsisDesc", "") if isinstance(synopsis, dict) else ""
    closing_date = hit.get("closingDate", "")
    if not closing_date and isinstance(synopsis, dict):
        closing_date = synopsis.get("responseDate", "")

    return FundingRecord(
        company_name=hit.get("title", ""),
        source="grants_gov",
        source_track="Grants & Funding",
        sector=hit.get("agencyName", ""),
        report_url=DETAIL_URL.format(opp_id),
        funding_type="government",
        # opportunityId prefix is the stable upsert key used by pipeline/airtable_sync.py
        notes=f"opportunityId:{opp_id} | closingDate:{closing_date} | {description}".strip(
            " |"
        ),
    )
