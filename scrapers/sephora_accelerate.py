"""Sephora Accelerate Playwright scraper — ESG & Corporate Partners.

Scrapes the program page (application status) and the /alumni page (launched
brands). Returns one record per alumni brand plus one for the program itself.
No login required. Upsert key: report_url.
"""

import logging
import re
from typing import List, Optional

from playwright.async_api import BrowserContext, async_playwright

from scrapers.base import FundingRecord
from scrapers.utils import random_delay, retry_async, tag_record

logger = logging.getLogger(__name__)

BASE_URL = "https://www.sephoraaccelerate.com"
PROGRAM_URL = f"{BASE_URL}/"
ALUMNI_URL = f"{BASE_URL}/alumni"
ROBOTS_URL = f"{BASE_URL}/robots.txt"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

OPEN_SIGNALS = [
    "apply now",
    "applications open",
    "now accepting",
    "submit your application",
    "applications are open",
    "apply today",
]
CLOSED_SIGNALS = [
    "applications closed",
    "applications are closed",
    "no longer accepting",
    "applications for this cycle",
]

# CSS selectors that match individual brand cards on the alumni page
_BRAND_CARD_SELECTOR = "[class*='brand'], [class*='card'], [class*='item'], [class*='alumni']"


async def scrape(headless: bool = True) -> List[FundingRecord]:
    """Return the program record plus one record per Sephora Accelerate alumni brand."""
    records: List[FundingRecord] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(user_agent=USER_AGENT)

        if not await _robots_allows(context):
            logger.warning("sephora_accelerate: robots.txt disallows scraping — skipping")
            await browser.close()
            return records

        try:
            record = await _scrape_program(context)
            if record:
                records.append(record)
        except Exception as exc:
            logger.error("sephora_accelerate: program scrape failed: %s", exc, exc_info=True)

        try:
            alumni = await _scrape_alumni(context)
            records.extend(alumni)
            logger.info("sephora_accelerate: found %d alumni brand records", len(alumni))
        except Exception as exc:
            logger.error("sephora_accelerate: alumni scrape failed: %s", exc, exc_info=True)

        await browser.close()

    if not records:
        logger.warning("sephora_accelerate: scraper returned 0 results")

    return records


async def _robots_allows(context: BrowserContext) -> bool:
    try:
        page = await context.new_page()
        await page.goto(ROBOTS_URL, timeout=15_000)
        text = await page.content()
        await page.close()
        return "Disallow: /" not in text.splitlines()[0] if text else True
    except Exception:
        return True


async def _scrape_program(context: BrowserContext) -> Optional[FundingRecord]:
    page = await context.new_page()
    await retry_async(
        lambda: page.goto(PROGRAM_URL, wait_until="networkidle", timeout=30_000)
    )

    body_el = await page.query_selector("body")
    body_text = (await body_el.inner_text()).strip() if body_el else ""

    desc_el = await page.query_selector(
        "main p, .hero-description, .program-description, "
        "section p, [class*='description'], [class*='intro']"
    )
    description = (await desc_el.inner_text()).strip() if desc_el else ""
    if not description and body_text:
        description = body_text[:500].replace("\n", " ")

    is_open = _detect_open_status(body_text)
    await page.close()

    record = FundingRecord(
        company_name="Sephora Accelerate",
        source="sephora_accelerate",
        source_track="ESG & Corporate Partners",
        sector="Beauty & Personal Care",
        report_url=PROGRAM_URL,
        funding_type="corporate_sponsor",
        is_open=is_open,
        notes=description[:500],
    )
    tag_record(record, f"{record.company_name} {record.notes}")
    return record


async def _scrape_alumni(context: BrowserContext) -> List[FundingRecord]:
    """Return one FundingRecord per alumni brand found on the /alumni page."""
    page = await context.new_page()
    await retry_async(
        lambda: page.goto(ALUMNI_URL, wait_until="networkidle", timeout=30_000)
    )
    # Scroll to trigger any lazy-loaded content
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(1500)

    cards = await page.eval_on_selector_all(
        _BRAND_CARD_SELECTOR,
        "els => els.map(e => e.innerText.trim()).filter(t => t && t.length > 1 && t.length < 60)",
    )
    await page.close()

    # Deduplicate while preserving order (page sometimes renders each name twice)
    seen: set = set()
    brand_names: List[str] = []
    skip = {"a-d", "e-h", "i-m", "n-r", "s-z", "available at sephora",
            "accelerated by sephora", "the brands"}
    for name in cards:
        key = name.lower().strip()
        if key not in seen and key not in skip:
            seen.add(key)
            brand_names.append(name)

    records: List[FundingRecord] = []
    for name in brand_names:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        record = FundingRecord(
            company_name=name,
            source="sephora_accelerate",
            source_track="ESG & Corporate Partners",
            sector="Beauty & Personal Care",
            report_url=f"{ALUMNI_URL}#{slug}",
            funding_type="corporate_sponsor",
            beauty_alignment=True,
            notes="Sephora Accelerate alumni — launched at Sephora",
        )
        tag_record(record, f"{record.company_name} {record.notes}")
        records.append(record)

    return records


def _detect_open_status(text: str) -> Optional[bool]:
    """Return True if open signals found, False if closed, None if indeterminate."""
    lower = text.lower()

    if any(signal in lower for signal in CLOSED_SIGNALS):
        return False

    if any(signal in lower for signal in OPEN_SIGNALS):
        return True

    logger.warning(
        "sephora_accelerate: could not determine open/closed status — "
        "no application signals found on page"
    )
    return None
