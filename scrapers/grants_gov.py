"""Grants.gov REST API scraper — government funding opportunities.

Searches open opportunities by sustainability/beauty keywords using the
Grants.gov legacy REST API (no auth required).
"""

import asyncio
import logging
import re
from typing import Any, Dict, List, Set

import requests

from scrapers.base import FundingRecord
from scrapers.utils import random_delay, retry_async, tag_record

logger = logging.getLogger(__name__)

SEARCH_URL = "https://apply07.grants.gov/grantsws/rest/opportunities/search/"
DETAIL_URL = "https://simpler.grants.gov/opportunity/{}"
PAGE_SIZE = 25

# Keywords searched sequentially; deduplication by opportunityId across all passes.
KEYWORDS = [
    "sustainable beauty",
    "clean beauty",
    "green cosmetics",
    "non-toxic personal care",
    "salon sustainability",
    "environmental health beauty",
    "clean beauty nonprofit",
    "green salon",
    "beauty industry sustainability",
    "toxic chemicals consumer products",
    "environmental justice personal care",
    "small business sustainability grant",
    "women owned business sustainability",
    "community health environmental",
    "clean product innovation",
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
            resp = requests.post(
                SEARCH_URL,
                json=p,
                timeout=30,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()

        data = await retry_async(lambda: asyncio.to_thread(_post))

        # Legacy API returns oppHits and hitCount at the top level
        hits: List[Dict[str, Any]] = data.get("oppHits", [])
        if not hits:
            break

        for hit in hits:
            opp_id = str(hit.get("id") or "")
            if not opp_id or opp_id in seen_ids:
                continue
            seen_ids.add(opp_id)
            record = _build_record(hit, opp_id)
            tag_record(record, f"{record.company_name} {record.notes}")
            records.append(record)

        total: int = data.get("hitCount", 0)
        offset += PAGE_SIZE
        if offset >= total:
            break

        await random_delay()

    return records


# Ordered list of (exact_phrase, points) pairs evaluated against the lowercased title.
# Score starts at 0; each matching phrase adds its points. Cap at 100.
# Longer/more-specific phrases are listed first so they can stack with shorter ones.
_SCORE_RULES: List[tuple] = [
    ("sustainable beauty",         30),
    ("clean beauty",               30),
    ("green beauty",               30),
    ("non-toxic",                  20),
    ("women-owned small business", 20),
    ("personal care",              50),
    ("hair care",                  50),
    ("nail care",                  40),
    ("small business",             10),
    ("cosmetic",                   50),
    ("beauty",                     50),
]

# Short words like "spa" need word-boundary matching to avoid false substring hits
# (e.g. "spa" inside "aerospace", "salon" inside "salmonella").
_WORD_BOUNDARY_RULES: List[tuple] = [
    ("spa",   40),
    ("salon", 50),
]


def _build_record(hit: Dict[str, Any], opp_id: str) -> FundingRecord:
    closing_date = hit.get("closeDate", "")
    title = (hit.get("title", "") or "").lower()

    score = sum(pts for phrase, pts in _SCORE_RULES if phrase in title)
    score += sum(
        pts for phrase, pts in _WORD_BOUNDARY_RULES
        if re.search(rf"\b{re.escape(phrase)}\b", title)
    )
    score = min(score, 100)

    base_notes = f"opportunityId:{opp_id} | closeDate:{closing_date}".strip(" |")
    notes = f"{base_notes} | score:{score}"

    return FundingRecord(
        company_name=hit.get("title", ""),
        source="grants_gov",
        source_track="Grants & Funding",
        sector=hit.get("agency", ""),
        report_url=DETAIL_URL.format(opp_id),
        funding_type="government",
        # opportunityId prefix is the stable upsert key used by pipeline/sheets_sync.py
        notes=notes,
    )
