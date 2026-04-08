"""GRI Sustainability Disclosure Database Playwright scraper — ESG & Corporate Partners.

Scrapes companies in consumer goods / personal care sectors that have published
GRI-based sustainability reports, from database.globalreporting.org.
No login required. Upsert key: report_url.
"""

import logging
from typing import List, Optional

from playwright.async_api import BrowserContext, Page, async_playwright

from scrapers.base import FundingRecord
from scrapers.utils import random_delay, retry_async, tag_record

logger = logging.getLogger(__name__)

SEARCH_URL = "https://database.globalreporting.org/search/"
ROBOTS_URL = "https://database.globalreporting.org/robots.txt"
BASE_URL = "https://database.globalreporting.org"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Sector filter values as they appear in the GRI database search UI.
# Adjust if the site uses different labels on first run.
TARGET_SECTORS = [
    "Consumer Goods",
    "Personal Care",
]


async def scrape(headless: bool = True) -> List[FundingRecord]:
    """Return GRI-reporting companies in consumer goods / personal care sectors."""
    records: List[FundingRecord] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(user_agent=USER_AGENT)

        if not await _robots_allows(context):
            logger.warning("gri: robots.txt disallows scraping — skipping")
            await browser.close()
            return records

        for sector in TARGET_SECTORS:
            try:
                sector_records = await _scrape_sector(context, sector)
                records.extend(sector_records)
            except Exception as exc:
                logger.error(
                    "gri: sector %r failed: %s", sector, exc, exc_info=True
                )
            await random_delay()

        await browser.close()

    if not records:
        logger.warning("gri: scraper returned 0 results")

    return records


async def _robots_allows(context: BrowserContext) -> bool:
    try:
        page = await context.new_page()
        await page.goto(ROBOTS_URL, timeout=15_000)
        text = await page.content()
        await page.close()
        return "Disallow: /search" not in text
    except Exception:
        return True


async def _scrape_sector(context: BrowserContext, sector: str) -> List[FundingRecord]:
    records: List[FundingRecord] = []
    page = await context.new_page()

    await retry_async(
        lambda: page.goto(SEARCH_URL, wait_until="networkidle", timeout=30_000)
    )

    # Apply sector filter via the search UI
    await _apply_sector_filter(page, sector)

    # Paginate through results
    while True:
        await page.wait_for_timeout(1_500)
        rows = await page.query_selector_all(
            "table tbody tr, .search-result, .report-row, [class*='result-item']"
        )

        if not rows:
            logger.warning("gri: no result rows found for sector %r", sector)
            break

        for row in rows:
            record = await _parse_row(row, sector)
            if record:
                tag_record(record, f"{record.company_name} {record.sector} {record.notes}")
                records.append(record)

        next_btn = await page.query_selector(
            "a[aria-label='Next page'], button[aria-label='Next'], "
            ".pagination__next:not([disabled]), [class*='next']:not([disabled])"
        )
        if not next_btn:
            break

        await next_btn.click()
        await page.wait_for_load_state("networkidle")
        await random_delay()

    await page.close()
    return records


async def _apply_sector_filter(page: Page, sector: str) -> None:
    """Attempt to filter results by sector using the search UI."""
    try:
        # Try a select dropdown first
        sector_select = await page.query_selector(
            "select[name*='sector' i], select[id*='sector' i], select[aria-label*='sector' i]"
        )
        if sector_select:
            await sector_select.select_option(label=sector)
            await page.wait_for_load_state("networkidle")
            return

        # Fall back to a text input filter
        sector_input = await page.query_selector(
            "input[placeholder*='sector' i], input[aria-label*='sector' i]"
        )
        if sector_input:
            await sector_input.fill(sector)
            await page.keyboard.press("Enter")
            await page.wait_for_load_state("networkidle")
            return

        logger.warning("gri: no sector filter control found for %r — scraping unfiltered", sector)
    except Exception as exc:
        logger.warning("gri: could not apply sector filter for %r: %s", sector, exc)


async def _parse_row(row, sector: str) -> Optional[FundingRecord]:
    try:
        # GRI database rows typically contain: org name, country, sector, year, report link
        name_el = await row.query_selector(
            "td:first-child, .org-name, [class*='organisation'], [class*='company']"
        )
        link_el = await row.query_selector("a[href]")
        year_el = await row.query_selector(
            "td:nth-child(4), .year, [class*='year'], [class*='date']"
        )
        country_el = await row.query_selector(
            "td:nth-child(2), .country, [class*='country']"
        )

        name = (await name_el.inner_text()).strip() if name_el else ""
        if not name:
            return None

        href = await link_el.get_attribute("href") if link_el else ""
        if href and not href.startswith("http"):
            href = BASE_URL + href

        year_text = (await year_el.inner_text()).strip() if year_el else ""
        country = (await country_el.inner_text()).strip() if country_el else ""

        year: Optional[int] = None
        if year_text.isdigit():
            year = int(year_text)

        return FundingRecord(
            company_name=name,
            source="gri",
            source_track="ESG & Corporate Partners",
            disclosure_status=True,
            sector=f"{sector} — {country}".strip(" —"),
            year_of_disclosure=year,
            report_url=href,
            funding_type="corporate_sponsor",
            is_open=None,
            notes=f"GRI sustainability report | year:{year_text} | country:{country}",
        )
    except Exception as exc:
        logger.warning("gri: failed to parse row: %s", exc)
        return None
