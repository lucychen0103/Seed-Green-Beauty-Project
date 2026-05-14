"""L'Oréal for the Future Playwright scraper — ESG & Corporate Partners.

Scrapes programme details and application status from the L'Oréal
commitments and responsibilities pages on loreal.com.
No login required. Upsert key: report_url.
"""

import logging
from typing import List, Optional

from playwright.async_api import BrowserContext, async_playwright

from scrapers.base import FundingRecord
from scrapers.utils import random_delay, retry_async, tag_record

logger = logging.getLogger(__name__)

PROGRAM_URL = "https://www.loreal.com/en/commitments-and-responsibilities/"
ROBOTS_URL = "https://www.loreal.com/robots.txt"
BASE_URL = "https://www.loreal.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Sub-pages likely to carry open-call or partnership language.
# Checked in addition to the main commitments page.
SUB_PATHS = [
    "/en/commitments-and-responsibilities/for-the-future/",
    "/en/commitments-and-responsibilities/sharing-beauty-with-all/",
    "/en/group/open-innovation/",
]

# Checked before open signals — "closed, apply for next cycle" → closed.
CLOSED_SIGNALS = [
    "applications closed",
    "applications are closed",
    "no longer accepting",
    "closed for applications",
    "cycle has ended",
    "cohort is full",
    "call is closed",
]
OPEN_SIGNALS = [
    "apply now",
    "applications open",
    "now accepting",
    "submit your application",
    "applications are open",
    "apply today",
    "open for applications",
    "accepting applications",
    "open call",
    "call for applications",
    "call for projects",
]


async def scrape(headless: bool = True) -> List[FundingRecord]:
    """Return L'Oréal for the Future programme record with current application status."""
    records: List[FundingRecord] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(user_agent=USER_AGENT)

        if not await _robots_allows(context):
            logger.warning("loreal: robots.txt disallows scraping — skipping")
            await browser.close()
            return records

        try:
            record = await _scrape_program(context)
            if record:
                records.append(record)
        except Exception as exc:
            logger.error("loreal: scrape failed: %s", exc, exc_info=True)

        await browser.close()

    if not records:
        logger.warning("loreal: scraper returned 0 results")

    return records


async def _robots_allows(context: BrowserContext) -> bool:
    try:
        page = await context.new_page()
        await page.goto(ROBOTS_URL, timeout=15_000)
        text = await page.content()
        await page.close()
        disallowed = [
            line for line in text.splitlines()
            if line.strip().startswith("Disallow:")
        ]
        return not any(
            "/en/commitments-and-responsibilities" in line for line in disallowed
        )
    except Exception:
        return True


async def _scrape_program(context: BrowserContext) -> Optional[FundingRecord]:
    # Scrape the main commitments page first
    page = await context.new_page()
    await retry_async(
        lambda: page.goto(PROGRAM_URL, wait_until="networkidle", timeout=30_000)
    )

    body_el = await page.query_selector("body")
    body_text = (await body_el.inner_text()).strip() if body_el else ""

    desc_el = await page.query_selector(
        "main p, .hero__description, .commitments-description, "
        "section p, [class*='description'], [class*='intro'], [class*='lead']"
    )
    description = (await desc_el.inner_text()).strip() if desc_el else ""
    if not description and body_text:
        description = body_text[:500].replace("\n", " ")

    await page.close()

    # Fetch known sub-pages to broaden open/closed signal coverage
    all_texts: List[str] = [body_text]
    for path in SUB_PATHS:
        url = BASE_URL + path
        try:
            sub_page = await context.new_page()
            await retry_async(
                lambda u=url: sub_page.goto(
                    u, wait_until="networkidle", timeout=30_000
                )
            )
            sub_body = await sub_page.query_selector("body")
            if sub_body:
                all_texts.append(await sub_body.inner_text())
            await sub_page.close()
            await random_delay()
        except Exception as exc:
            logger.warning("loreal: failed to fetch sub-page %s: %s", url, exc)

    combined_text = " ".join(all_texts)
    is_open = _detect_open_status(combined_text)

    record = FundingRecord(
        company_name="L'Oréal for the Future",
        source="loreal",
        source_track="ESG & Corporate Partners",
        sector="Beauty & Personal Care",
        report_url=PROGRAM_URL,
        funding_type="corporate_sponsor",
        is_open=is_open,
        notes=description[:500],
    )
    tag_record(record, f"{record.company_name} {record.notes} {combined_text[:300]}")
    return record


def _detect_open_status(text: str) -> Optional[bool]:
    """Return True if open signals found, False if closed, None if indeterminate."""
    lower = text.lower()

    if any(signal in lower for signal in CLOSED_SIGNALS):
        return False

    if any(signal in lower for signal in OPEN_SIGNALS):
        return True

    logger.warning(
        "loreal: could not determine open/closed status — "
        "no application signals found on page"
    )
    return None
