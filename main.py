"""ESG Funding Intelligence Platform — main entry point.

Runs all scrapers sequentially, syncs non-empty results to Google Sheets,
and writes a sanitised data.json for the static dashboard.

Usage:
    python main.py              # run all sources
    python main.py grants_gov   # run one source by name
"""

import asyncio
import dataclasses
import json
import logging
import sys
from typing import Callable, Coroutine, Dict, List

from scrapers.base import FundingRecord
from scrapers import (
    bcorp,
    california_hcd,
    epa_grants,
    grants_gov,
    gri,
    loreal,
    propublica,
    sephora_accelerate,
    unilever_foundry,
)
from pipeline.sheets_sync import sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

DATA_JSON_PATH = "data.json"

# Ordered by build priority; each value is a zero-argument async callable.
SCRAPERS: Dict[str, Callable[[], Coroutine]] = {
    "grants_gov":          grants_gov.scrape,
    "bcorp":               bcorp.scrape,
    "propublica":          propublica.scrape,
    "epa_grants":          epa_grants.scrape,
    "california_hcd":      california_hcd.scrape,
    "sephora_accelerate":  sephora_accelerate.scrape,
    "unilever_foundry":    unilever_foundry.scrape,
    "loreal":              loreal.scrape,
    "gri":                 gri.scrape,
}


async def run_all(source: str = "all") -> None:
    """Run scrapers for the given source (or all), sync to Sheets, write data.json."""
    if source != "all" and source not in SCRAPERS:
        logger.error(
            "Unknown source '%s'. Valid options: all, %s",
            source,
            ", ".join(SCRAPERS),
        )
        sys.exit(1)

    all_records: List[FundingRecord] = []

    targets = SCRAPERS if source == "all" else {source: SCRAPERS[source]}

    for name, scrape_fn in targets.items():
        logger.info("Running scraper: %s", name)
        try:
            records = await scrape_fn()
        except Exception as exc:
            logger.error(
                "Scraper '%s' raised an unhandled exception: %s — skipping",
                name,
                exc,
                exc_info=True,
            )
            continue

        if not records:
            # The individual scraper already logged a warning; skip sync for this source.
            logger.warning("Scraper '%s' returned 0 results — skipping sync", name)
            continue

        logger.info("Scraper '%s' returned %d records", name, len(records))
        all_records.extend(records)

    if not all_records:
        logger.warning("No records from any source — Sheets sync and data.json skipped")
        return

    logger.info("Syncing %d total records to Google Sheets...", len(all_records))
    try:
        sync(all_records)
    except Exception as exc:
        logger.error("Sheets sync failed: %s", exc, exc_info=True)

    logger.info("Writing %s...", DATA_JSON_PATH)
    _write_data_json(all_records)
    logger.info("Done.")


def _write_data_json(records: List[FundingRecord]) -> None:
    """Serialise records to data.json for the static dashboard."""
    payload = [dataclasses.asdict(r) for r in records]
    with open(DATA_JSON_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    source = sys.argv[1] if len(sys.argv) > 1 else "all"
    asyncio.run(run_all(source))
