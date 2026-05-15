"""CDP scraper — uses CDP Open Data Portal (data.cdp.net).

Downloads the publicly available Global 500 Emissions & Response Status
datasets (2011–2013) via the Socrata CSV API and filters for known
beauty/personal care companies by brand name.

No login required. Full questionnaire responses require a paid CDP
subscription; this scraper captures publicly available disclosure scores
and performance bands only.

Socrata dataset IDs:
  cxp6-pzzb — 2011 Global 500
  4hek-p74b — 2012 Global 500
  marp-zazk — 2013 Global 500
"""

import csv
import io
import logging
import sys
from datetime import datetime, timezone
from typing import List

import requests

from scrapers.base import FundingRecord
from scrapers.utils import tag_record

logger = logging.getLogger(__name__)

SOCRATA_BASE = "https://data.cdp.net/api/views/{dataset_id}/rows.csv?accessType=DOWNLOAD"

# (dataset_id, year) ordered oldest → newest so dedup keeps the most recent
DATASETS = [
    ("cxp6-pzzb", 2011),
    ("4hek-p74b", 2012),
    ("marp-zazk", 2013),
]

REQUEST_TIMEOUT = 30

# Curated list of beauty/personal care company name fragments.
# Kept specific to avoid false positives (e.g. "johnson controls", "gas natural").
BEAUTY_BRAND_FRAGMENTS = (
    "l'oreal",
    "loreal",
    "unilever",
    "procter & gamble",
    "procter and gamble",
    "estee lauder",
    "beiersdorf",
    "shiseido",
    "kao corporation",
    "coty inc",
    "avon products",
    "natura ",          # trailing space avoids "natural resources"
    "amorepacific",
    "lvmh",
    "interparfums",
    "revlon",
    "elizabeth arden",
    "clarins",
    "kimberly-clark",
    "colgate-palmolive",
    "colgate palmolive",
    "reckitt benckiser",
    "johnson & johnson",
    "henkel ag",
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_beauty(company_name: str) -> bool:
    name = company_name.lower()
    return any(frag in name for frag in BEAUTY_BRAND_FRAGMENTS)


def _map_row(row: dict, year: int) -> FundingRecord:
    name = row.get("Company Name ", "").strip()
    band = row.get("Performance Band", "").strip()
    record = FundingRecord(
        company_name=name,
        source="cdp",
        source_track="ESG & Corporate Partners",
        disclosure_status=True,
        score_or_rating=row.get("Disclosure Score", "").strip(),
        sector="Personal Care / Beauty",
        year_of_disclosure=year,
        report_url=f"https://www.cdp.net/en/responses?queries%5Bname%5D={requests.utils.quote(name)}",
        funding_type="corporate_sponsor",
        scraped_at=_now_utc(),
        notes=f"Performance Band: {band} | CDP open data {year}",
    )
    tag_record(record, f"{name} carbon disclosure CDP signatory band:{band}")
    return record


def _fetch_dataset(dataset_id: str, year: int) -> List[FundingRecord]:
    url = SOCRATA_BASE.format(dataset_id=dataset_id)
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(resp.text)))
        matches = [_map_row(r, year) for r in rows if _is_beauty(r.get("Company Name ", ""))]
        logger.info("cdp: %d dataset: %d companies → %d beauty matches", year, len(rows), len(matches))
        return matches
    except requests.exceptions.RequestException as exc:
        logger.error("cdp: failed to fetch %d dataset: %s", year, exc)
        return []


async def scrape() -> List[FundingRecord]:
    """Return CDP-disclosed beauty/personal care companies as FundingRecords."""
    seen: dict[str, FundingRecord] = {}  # company_name → record; later years overwrite earlier

    for dataset_id, year in DATASETS:
        for record in _fetch_dataset(dataset_id, year):
            seen[record.company_name.lower()] = record

    records = list(seen.values())
    logger.info("cdp: %d unique beauty companies across all years", len(records))

    if not records:
        logger.warning("cdp: scraper returned 0 results")

    return records

