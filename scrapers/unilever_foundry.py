"""Unilever Foundry Playwright scraper — ESG & Corporate Partners.

Scrapes program details and application status from unileverfoundry.com.
No login required. Upsert key: report_url.
"""

import logging
from typing import List, Optional

from playwright.async_api import BrowserContext, async_playwright

from scrapers.base import FundingRecord
from scrapers.utils import random_delay, retry_async, tag_record

logger = logging.getLogger(__name__)

PROGRAM_URL = "https://www.unileverfoundry.com/"
ROBOTS_URL = "https://www.unileverfoundry.com/robots.txt"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Checked before open signals — "closed, apply for next cycle" should read as closed.
CLOSED_SIGNALS = [
    "applications closed",
    "applications are closed",
    "no longer accepting",
    "closed for applications",
    "cycle has ended",
    "cohort is full",
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
]


async def scrape(headless: bool = True) -> List[FundingRecord]:
    """Return Unilever Foundry program record with current application status."""
    records: List[FundingRecord] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(user_agent=USER_AGENT)

        if not await _robots_allows(context):
            logger.warning("unilever_foundry: robots.txt disallows scraping — skipping")
            await browser.close()
            return records

        try:
            record = await _scrape_program(context)
            if record:
                records.append(record)
        except Exception as exc:
            logger.error(
                "unilever_foundry: scrape failed: %s", exc, exc_info=True
            )

        await browser.close()

    if not records:
        logger.warning("unilever_foundry: scraper returned 0 results")

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

    # Unilever Foundry may list multiple sub-programmes (e.g. Grow, Scale).
    # Capture the first meaningful paragraph as the programme description.
    desc_el = await page.query_selector(
        "main p, .hero__description, .programme-description, "
        "section p, [class*='description'], [class*='intro'], [class*='about']"
    )
    description = (await desc_el.inner_text()).strip() if desc_el else ""
    if not description and body_text:
        description = body_text[:500].replace("\n", " ")

    # Check for sub-programme pages linked from the homepage and follow them
    # to get a richer open/closed signal if the homepage is ambiguous.
    sub_links = await _collect_sub_programme_links(page)
    sub_texts: List[str] = [body_text]

    for href in sub_links:
        try:
            sub_page = await context.new_page()
            await retry_async(
                lambda h=href: sub_page.goto(
                    h, wait_until="networkidle", timeout=30_000
                )
            )
            sub_body = await sub_page.query_selector("body")
            if sub_body:
                sub_texts.append(await sub_body.inner_text())
            await sub_page.close()
            await random_delay()
        except Exception as exc:
            logger.warning("unilever_foundry: failed to fetch sub-page %s: %s", href, exc)

    combined_text = " ".join(sub_texts)
    is_open = _detect_open_status(combined_text)

    await page.close()

    record = FundingRecord(
        company_name="Unilever Foundry",
        source="unilever_foundry",
        source_track="ESG & Corporate Partners",
        sector="Beauty & Personal Care / CPG",
        report_url=PROGRAM_URL,
        funding_type="corporate_sponsor",
        is_open=is_open,
        notes=description[:500],
    )
    tag_record(record, f"{record.company_name} {record.notes} {combined_text[:300]}")
    return record


async def _collect_sub_programme_links(page) -> List[str]:
    """Return internal links that likely point to sub-programme pages."""
    hrefs: List[str] = []
    anchors = await page.query_selector_all("a[href]")
    for anchor in anchors:
        href = await anchor.get_attribute("href")
        if not href:
            continue
        if href.startswith("#") or href.startswith("mailto:"):
            continue
        if href.startswith("http") and "unileverfoundry.com" not in href:
            continue
        if not href.startswith("http"):
            href = PROGRAM_URL.rstrip("/") + "/" + href.lstrip("/")
        if href != PROGRAM_URL and href not in hrefs:
            hrefs.append(href)
    # Limit to avoid crawling the entire site
    return hrefs[:5]


def _detect_open_status(text: str) -> Optional[bool]:
    """Return True if open signals found, False if closed, None if indeterminate."""
    lower = text.lower()

    if any(signal in lower for signal in CLOSED_SIGNALS):
        return False

    if any(signal in lower for signal in OPEN_SIGNALS):
        return True

    logger.warning(
        "unilever_foundry: could not determine open/closed status — "
        "no application signals found on page"
    )
    return None
