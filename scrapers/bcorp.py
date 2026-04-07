"""B Corp Directory Playwright scraper — ESG & Corporate Partners.

Scrapes certified B Corp companies in the Personal Care & Beauty sector
from bcorporation.net/en-us/find-a-b-corp/.
"""

import logging
from typing import List, Optional

from playwright.async_api import BrowserContext, async_playwright

from scrapers.base import FundingRecord
from scrapers.utils import random_delay, retry_async, tag_record

logger = logging.getLogger(__name__)

DIRECTORY_URL = "https://www.bcorporation.net/en-us/find-a-b-corp/"
ROBOTS_URL = "https://www.bcorporation.net/robots.txt"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

INDUSTRIES = ["Personal Care & Beauty", "Cleantech"]


async def scrape(headless: bool = True) -> List[FundingRecord]:
    """Return B Corp certified companies in beauty/personal care sectors."""
    records: List[FundingRecord] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(user_agent=USER_AGENT)

        if not await _robots_allows(context):
            logger.warning("bcorp: robots.txt disallows scraping — skipping")
            await browser.close()
            return records

        for industry in INDUSTRIES:
            try:
                industry_records = await _scrape_industry(context, industry)
                records.extend(industry_records)
            except Exception as exc:
                logger.error(
                    "bcorp: industry %r failed: %s", industry, exc, exc_info=True
                )
            await random_delay()

        await browser.close()

    if not records:
        logger.warning("bcorp: scraper returned 0 results")

    return records


async def _robots_allows(context: BrowserContext) -> bool:
    """Return True if robots.txt permits scraping the directory path."""
    try:
        page = await context.new_page()
        await page.goto(ROBOTS_URL, timeout=15_000)
        text = await page.content()
        await page.close()
        return "Disallow: /en-us/find-a-b-corp/" not in text
    except Exception:
        return True


async def _scrape_industry(context: BrowserContext, industry: str) -> List[FundingRecord]:
    records: List[FundingRecord] = []
    page = await context.new_page()

    await retry_async(
        lambda: page.goto(DIRECTORY_URL, wait_until="networkidle", timeout=30_000)
    )

    # Apply industry filter if a filter input is present
    try:
        await page.fill(
            'input[placeholder*="industry" i], input[aria-label*="industry" i]',
            industry,
        )
        await page.keyboard.press("Enter")
        await page.wait_for_load_state("networkidle")
    except Exception:
        logger.warning(
            "bcorp: could not apply industry filter for %r — scraping unfiltered",
            industry,
        )

    # Paginate through result cards
    while True:
        await page.wait_for_timeout(1_500)
        cards = await page.query_selector_all(
            ".company-card, .directory-result, article[class*='company'], li[class*='result']"
        )

        for card in cards:
            record = await _parse_card(card, industry)
            if record:
                tag_record(record, f"{record.company_name} {record.sector} {record.notes}")
                records.append(record)

        next_btn = await page.query_selector(
            "a[aria-label='Next page'], button[aria-label='Next'], "
            ".pagination__next:not([disabled])"
        )
        if not next_btn:
            break

        await next_btn.click()
        await page.wait_for_load_state("networkidle")
        await random_delay()

    await page.close()
    return records


async def _parse_card(card, industry: str) -> Optional[FundingRecord]:
    try:
        name_el = await card.query_selector("h2, h3, .company-name, [class*='name']")
        link_el = await card.query_selector("a")
        score_el = await card.query_selector(".score, .b-impact-score, [class*='score']")
        sector_el = await card.query_selector(".industry, .sector, [class*='industry']")

        name = (await name_el.inner_text()).strip() if name_el else ""
        if not name:
            return None

        href = await link_el.get_attribute("href") if link_el else ""
        if href and not href.startswith("http"):
            href = "https://www.bcorporation.net" + href

        score = (await score_el.inner_text()).strip() if score_el else ""
        sector = (await sector_el.inner_text()).strip() if sector_el else industry

        return FundingRecord(
            company_name=name,
            source="bcorp",
            source_track="ESG & Corporate Partners",
            disclosure_status=True,
            score_or_rating=score,
            sector=sector,
            report_url=href,
            funding_type="corporate_sponsor",
            is_open=None,
        )
    except Exception as exc:
        logger.warning("bcorp: failed to parse card: %s", exc)
        return None
