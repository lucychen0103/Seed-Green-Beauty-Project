"""Shared utilities for all ESG Funding Intelligence scrapers."""

import asyncio
import logging
import random
import sys
from typing import Callable, List, TypeVar

from scrapers.base import FundingRecord

logger = logging.getLogger(__name__)

T = TypeVar("T")

BEAUTY_TERMS: List[str] = [
    "beauty",
    "personal care",
    "cosmetics",
    "skincare",
    "clean beauty",
    "eco packaging",
    "haircare",
    "fragrance",
    "CPG",
    "consumer packaged goods",
]

ESG_TERMS: List[str] = [
    "carbon neutral",
    "circular economy",
    "Scope 3",
    "decarbonization",
    "sustainability",
    "CSR",
    "B Corp",
    "ESG",
    "net zero",
    "climate",
    "sustainable beauty",
    "clean beauty innovation",
]


async def retry_async(fn: Callable, retries: int = 3) -> T:
    """Call an async callable up to `retries` times with exponential backoff.

    Raises the last exception if all attempts fail.
    """
    delay = 1.0
    last_exc: Exception = RuntimeError("retry_async called with 0 retries")
    for attempt in range(1, retries + 1):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                logger.warning(
                    "Attempt %d/%d failed: %s — retrying in %.0fs",
                    attempt,
                    retries,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay *= 2
            else:
                logger.error(
                    "All %d attempts failed: %s", retries, exc, exc_info=True
                )
    raise last_exc


async def random_delay(min_s: float = 2.0, max_s: float = 5.0) -> None:
    """Sleep for a random duration between paginated requests."""
    await asyncio.sleep(random.uniform(min_s, max_s))


def tag_record(record: FundingRecord, text: str) -> FundingRecord:
    """Set beauty_alignment and sustainability_keywords from scraped text.

    This is the single authoritative place these fields are set.
    Every scraper must call this before appending a record to results.
    """
    lower = text.lower()
    record.beauty_alignment = any(t.lower() in lower for t in BEAUTY_TERMS)
    record.sustainability_keywords = [t for t in ESG_TERMS if t.lower() in lower]
    return record
