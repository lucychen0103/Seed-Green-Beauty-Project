"""California HCD Playwright scraper — government funding opportunities.

Scrapes current grant and funding opportunities from the California
Department of Housing and Community Development (hcd.ca.gov/grants-and-funding).
No login required. Upsert key: composite source + company_name.
"""

import logging
import re
from typing import List, Optional

from playwright.async_api import BrowserContext, Page, async_playwright

from scrapers.base import FundingRecord
from scrapers.utils import random_delay, retry_async, tag_record

logger = logging.getLogger(__name__)

GRANTS_URL = "https://www.hcd.ca.gov/grants-and-funding"
ROBOTS_URL = "https://www.hcd.ca.gov/robots.txt"
BASE_URL = "https://www.hcd.ca.gov"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def scrape(headless: bool = True) -> List[FundingRecord]:
    """Return current California HCD grant and funding opportunities."""
    records: List[FundingRecord] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(user_agent=USER_AGENT)

        if not await _robots_allows(context):
            logger.warning("california_hcd: robots.txt disallows scraping — skipping")
            await browser.close()
            return records

        try:
            records = await _scrape_listings(context)
        except Exception as exc:
            logger.error("california_hcd: scrape failed: %s", exc, exc_info=True)

        await browser.close()

    if not records:
        logger.warning("california_hcd: scraper returned 0 results")

    return records


async def _robots_allows(context: BrowserContext) -> bool:
    """Return True if robots.txt permits scraping the grants path."""
    try:
        page = await context.new_page()
        await page.goto(ROBOTS_URL, timeout=15_000)
        text = await page.content()
        await page.close()
        return "Disallow: /grants-and-funding" not in text
    except Exception:
        return True


async def _scrape_listings(context: BrowserContext) -> List[FundingRecord]:
    records: List[FundingRecord] = []
    page = await context.new_page()

    await retry_async(
        lambda: page.goto(GRANTS_URL, wait_until="networkidle", timeout=30_000)
    )

    grant_links = await _collect_grant_links(page)
    logger.info("california_hcd: found %d grant links", len(grant_links))

    for href in grant_links:
        try:
            record = await _scrape_grant_page(context, href)
            if record:
                tag_record(record, f"{record.company_name} {record.notes}")
                records.append(record)
            await random_delay()
        except Exception as exc:
            logger.warning("california_hcd: failed to scrape %s: %s", href, exc)

    await page.close()
    return records


async def _collect_grant_links(page: Page) -> List[str]:
    """Return absolute URLs of individual grant or program pages."""
    hrefs: List[str] = []

    anchors = await page.query_selector_all(
        "main a[href], .content a[href], article a[href], "
        ".field--name-body a[href], .view-content a[href]"
    )
    for anchor in anchors:
        href = await anchor.get_attribute("href")
        if not href:
            continue
        # Skip anchors, external links, and non-grant paths
        if href.startswith("#") or href.startswith("mailto:"):
            continue
        if href.startswith("http") and "hcd.ca.gov" not in href:
            continue
        if not href.startswith("http"):
            href = BASE_URL + href
        if href not in hrefs:
            hrefs.append(href)

    return hrefs


async def _scrape_grant_page(
    context: BrowserContext, url: str
) -> Optional[FundingRecord]:
    """Scrape an individual HCD grant or program page."""
    page = await context.new_page()

    await retry_async(
        lambda: page.goto(url, wait_until="networkidle", timeout=30_000)
    )

    title_el = await page.query_selector("h1, h2.page-title, .field--name-title")
    title = (await title_el.inner_text()).strip() if title_el else ""
    if not title:
        await page.close()
        return None

    body_el = await page.query_selector(
        "main, .main-content, article, .field--name-body"
    )
    body_text = (await body_el.inner_text()).strip() if body_el else ""

    deadline = _extract_deadline(body_text)
    description = body_text[:500].replace("\n", " ") if body_text else ""

    await page.close()

    return FundingRecord(
        company_name=title,
        source="california_hcd",
        source_track="Grants & Funding",
        sector="California Housing & Community Development",
        report_url=url,
        funding_type="government",
        is_open=None,
        notes=f"deadline:{deadline} | {description}".strip(" |"),
    )


def _extract_deadline(text: str) -> str:
    """Extract a deadline date string from page text if present."""
    patterns = [
        r"deadline[:\s]+([A-Za-z]+ \d{1,2},?\s*\d{4})",
        r"due date[:\s]+([A-Za-z]+ \d{1,2},?\s*\d{4})",
        r"applications? due[:\s]+([A-Za-z]+ \d{1,2},?\s*\d{4})",
        r"closing date[:\s]+([A-Za-z]+ \d{1,2},?\s*\d{4})",
        r"notice of funding availability[:\s]+.*?(\w+ \d{1,2},?\s*\d{4})",
        r"(\d{1,2}/\d{1,2}/\d{4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""
