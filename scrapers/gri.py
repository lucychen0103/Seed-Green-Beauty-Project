"""GRI Sustainability Disclosure Database Playwright scraper — ESG & Corporate Partners.

Scrapes companies in sustainability / ESG / CSR sectors that have published
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

SEARCH_KEYWORDS = [
    "Sustainability",
    "ESG",
    "CSR",
    "Social Responsibility",
    "Corporate Social Responsibility",
    "Environmental",
    "Climate",
    "Green",
    "Impact",
    "Ethical",
]


async def scrape(headless: bool = True) -> List[FundingRecord]:
    """Return GRI-reporting companies matching ESG/sustainability keywords."""
    records: List[FundingRecord] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(user_agent=USER_AGENT)

        if not await _robots_allows(context):
            logger.warning("gri: robots.txt disallows scraping — skipping")
            await browser.close()
            return records

        seen_urls: set = set()
        for keyword in SEARCH_KEYWORDS:
            try:
                kw_records = await _scrape_keyword(context, keyword, seen_urls)
                records.extend(kw_records)
            except Exception as exc:
                logger.error(
                    "gri: keyword %r failed: %s", keyword, exc, exc_info=True
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


async def _scrape_keyword(
    context: BrowserContext, keyword: str, seen_urls: set
) -> List[FundingRecord]:
    records: List[FundingRecord] = []
    page = await context.new_page()

    await retry_async(
        lambda: page.goto(SEARCH_URL, wait_until="networkidle", timeout=30_000)
    )
    await page.wait_for_timeout(2_000)

    await _run_keyword_search(page, keyword)

    # Wait for results to appear
    try:
        await page.wait_for_selector(
            "table tbody tr, [class*='result'], [role='row'], li[class*='result']",
            timeout=10_000,
        )
    except Exception:
        logger.warning("gri: no results selector appeared for keyword %r", keyword)
        await _save_debug_snapshot(page, keyword)
        await page.close()
        return records

    # Paginate through results
    while True:
        await page.wait_for_timeout(1_000)
        rows = await page.query_selector_all(
            "table tbody tr, .search-result, .report-row, "
            "[class*='result-item'], [role='row'], li[class*='result']"
        )

        if not rows:
            logger.warning("gri: no result rows found for keyword %r", keyword)
            await _save_debug_snapshot(page, keyword)
            break

        logger.info("gri: %d rows for keyword %r", len(rows), keyword)

        for row in rows:
            record = await _parse_row(row, keyword)
            if record and record.report_url not in seen_urls:
                seen_urls.add(record.report_url)
                tag_record(record, f"{record.company_name} {record.sector} {record.notes}")
                records.append(record)

        next_btn = await page.query_selector(
            "button:has-text('Next'), a:has-text('Next'), "
            "[aria-label*='next' i]:not([disabled])"
        )
        if not next_btn:
            break

        await next_btn.click()
        await page.wait_for_load_state("networkidle")
        await random_delay()

    await page.close()
    return records


async def _run_keyword_search(page: Page, keyword: str) -> None:
    """Submit keyword as a text search query."""
    try:
        search_input = await page.query_selector(
            "input[type='search'], input[placeholder*='search' i], "
            "input[placeholder*='organization' i], input[placeholder*='company' i], "
            "[role='searchbox'], input[name*='search' i], input[id*='search' i]"
        )
        if search_input:
            await search_input.fill(keyword)
            await search_input.press("Enter")
            await page.wait_for_load_state("networkidle")
            return

        # Fall back to appending query param
        url = f"{SEARCH_URL}?q={keyword.replace(' ', '+')}"
        await page.goto(url, wait_until="networkidle", timeout=30_000)
        logger.info("gri: no search input found for %r — navigated to %s", keyword, url)
    except Exception as exc:
        logger.warning("gri: keyword search failed for %r: %s", keyword, exc)


async def _save_debug_snapshot(page: Page, keyword: str) -> None:
    """Save screenshot and HTML for offline selector inspection."""
    try:
        safe = keyword.replace(" ", "_").lower()
        await page.screenshot(path=f"debug_gri_{safe}.png", full_page=True)
        html = await page.content()
        with open(f"debug_gri_{safe}.html", "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("gri: debug snapshot saved for keyword %r", keyword)
    except Exception as exc:
        logger.warning("gri: could not save debug snapshot: %s", exc)


async def _parse_row(row, keyword: str) -> Optional[FundingRecord]:
    try:
        name_el = await row.query_selector(
            "td:first-child, .org-name, [class*='organisation'], [class*='company'], "
            "[class*='organization'], [class*='name']"
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
            sector=f"{keyword} — {country}".strip(" —"),
            year_of_disclosure=year,
            report_url=href,
            funding_type="corporate_sponsor",
            is_open=None,
            notes=f"GRI sustainability report | keyword:{keyword} | year:{year_text} | country:{country}",
        )
    except Exception as exc:
        logger.warning("gri: failed to parse row: %s", exc)
        return None
