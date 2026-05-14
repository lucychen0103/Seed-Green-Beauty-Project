"""B Corp Directory Playwright scraper — ESG & Corporate Partners.

Scrapes certified B Corp companies in the Personal Care & Beauty sector
from bcorporation.net/en-us/find-a-b-corp/.

The site is a Next.js/React app. Confirmed data-testid attributes (2026-04):
  search-input      — the text input inside the search box
  search-button     — submit button
  profile-link      — one per company result (anchor element)
  company-name-desktop — company name text, child of profile-link
"""

import logging
import urllib.parse
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

RESULTS_PER_PAGE = 25
MAX_PAGES = 20  # safety cap: 20 × 25 = 500 per industry

INDUSTRIES = [
    "Personal Care & Beauty",
    "Cleantech",
    "Health & Wellness",
    "Retail",
    "Consumer Goods",
    "Fashion & Apparel",
    "Food & Beverage",
    "Education",
    "Environmental Services",
    "Hospitality",
]


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

        seen_urls: set = set()
        for industry in INDUSTRIES:
            try:
                industry_records = await _scrape_industry(context, industry, seen_urls)
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


async def _scrape_industry(
    context: BrowserContext, industry: str, seen_urls: set
) -> List[FundingRecord]:
    records: List[FundingRecord] = []
    page = await context.new_page()
    encoded = urllib.parse.quote(industry)

    page_num = 0
    while page_num <= MAX_PAGES:
        page_param = f"&page={page_num}" if page_num > 0 else ""
        url = (
            f"{DIRECTORY_URL}?query={encoded}"
            f"&sortBy=companies-production-en-us{page_param}"
        )

        await retry_async(
            lambda u=url: page.goto(u, wait_until="networkidle", timeout=60_000)
        )
        await page.wait_for_timeout(3_000)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(2_000)

        card_els = await page.query_selector_all('[data-testid="profile-link"]')

        if not card_els:
            break

        new_count = 0
        for card in card_els:
            record = await _parse_card(card, industry)
            if record and record.report_url not in seen_urls:
                seen_urls.add(record.report_url)
                tag_record(record, f"{record.company_name} {record.sector} {record.notes}")
                records.append(record)
                new_count += 1

        logger.info(
            "bcorp: page %d — %d results (%d new) for %r",
            page_num, len(card_els), new_count, industry,
        )

        # Stop when the site returns a partial page (last page) or all results already seen
        if len(card_els) < RESULTS_PER_PAGE or new_count == 0:
            break

        page_num += 1

    logger.info(
        "bcorp: %d unique results for %r across %d page(s)",
        len(records), industry, page_num + 1,
    )
    await page.close()
    return records


async def _parse_card(card, industry: str) -> Optional[FundingRecord]:
    """Parse a [data-testid="profile-link"] anchor element into a FundingRecord."""
    try:
        # Company name
        name_el = await card.query_selector('[data-testid="company-name-desktop"]')
        name = (await name_el.inner_text()).strip() if name_el else ""
        if not name:
            # Fallback: any heading or the link text itself
            name = (await card.inner_text()).strip().splitlines()[0]
        if not name:
            return None

        # Profile URL — the card itself is the anchor
        href = await card.get_attribute("href") or ""
        if href and not href.startswith("http"):
            href = "https://www.bcorporation.net" + href

        # Optional: score / sector within card
        score_el = await card.query_selector('[class*="score"], [data-testid*="score"]')
        sector_el = await card.query_selector('[data-testid*="industry"], [data-testid*="sector"]')

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
