"""Enrich the B Corp tab with each company's own website URL.

Visits every report_url in the B Corp tab using Playwright, extracts the
company's external website link from the profile page, and writes the results
into a new 'company_website' column (appended after the last existing column).

Usage:
    python _enrich_bcorp_websites.py
"""

import asyncio
import json
import logging
import os
import sys
import time

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright

load_dotenv(dotenv_path=".env")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

NEW_COL = "company_website"

# Delay between page loads to be respectful
PAGE_DELAY_S = 1.5


def _get_spreadsheet() -> gspread.Spreadsheet:
    raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(os.environ["SPREADSHEET_ID"])


SKIP_DOMAINS = {
    # B Corp infrastructure
    "bcorporation.net",
    "bcorpmonth.com",
    "bimpactassessment.net",
    "amazonaws.com",   # S3 PDFs / reports
    "provoc.me",       # B Corp web design partner shown in footer
    # Social media
    "twitter.com",
    "x.com",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "youtube.com",
    "tiktok.com",
    "pinterest.com",
    "threads.net",
}


async def _extract_website(page, url: str) -> str:
    """Navigate to a B Corp profile page and return the company's own website URL."""
    from urllib.parse import urlparse

    try:
        await page.goto(url, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(2_000)

        anchors = await page.query_selector_all("a[href]")
        seen: set[str] = set()

        for anchor in anchors:
            href = (await anchor.get_attribute("href") or "").strip()
            if not href.startswith("http"):
                continue
            if href in seen:
                continue
            seen.add(href)

            parsed = urlparse(href)
            domain = parsed.netloc.lower().lstrip("www.")

            # Skip any B Corp infra or social domains
            if any(domain == sd or domain.endswith("." + sd) for sd in SKIP_DOMAINS):
                continue

            # The company website is the first clean external link remaining
            return href

        return ""
    except Exception as exc:
        logger.warning("Failed to load %s: %s", url, exc)
        return ""


async def run() -> None:
    spreadsheet = _get_spreadsheet()
    ws = spreadsheet.worksheet("B Corp")
    rows = ws.get_all_values()

    if not rows:
        logger.error("B Corp tab is empty")
        return

    header = rows[0]
    url_col_idx = header.index("report_url")
    name_col_idx = header.index("company_name")

    # Check if company_website column already exists
    if NEW_COL in header:
        website_col_idx = header.index(NEW_COL)
        logger.info("'%s' column already exists at index %d — will update in place", NEW_COL, website_col_idx)
    else:
        website_col_idx = len(header)  # append after last column
        # Expand the sheet grid if needed to accommodate the new column
        current_cols = ws.col_count
        if website_col_idx + 1 > current_cols:
            ws.resize(rows=ws.row_count, cols=website_col_idx + 1)
            logger.info("Resized sheet to %d columns", website_col_idx + 1)
        # Write the header for the new column
        col_letter = _col_letter(website_col_idx + 1)
        ws.update(values=[[NEW_COL]], range_name=f"{col_letter}1")
        logger.info("Added '%s' column header at column %s", NEW_COL, col_letter)

    data_rows = rows[1:]
    total = len(data_rows)
    logger.info("Processing %d B Corp profiles...", total)

    # Collect existing website values to skip already-populated rows
    existing_websites = [
        row[website_col_idx].strip() if website_col_idx < len(row) else ""
        for row in data_rows
    ]

    results: list[str] = list(existing_websites)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        for i, row in enumerate(data_rows):
            # Skip rows already filled
            if existing_websites[i]:
                logger.info("[%d/%d] Skipping %r (already has website)", i + 1, total, row[name_col_idx])
                continue

            profile_url = row[url_col_idx].strip() if url_col_idx < len(row) else ""
            name = row[name_col_idx].strip() if name_col_idx < len(row) else ""

            if not profile_url:
                logger.warning("[%d/%d] No profile URL for %r — skipping", i + 1, total, name)
                results[i] = ""
                continue

            logger.info("[%d/%d] Fetching %r -> %s", i + 1, total, name, profile_url)
            website = await _extract_website(page, profile_url)
            results[i] = website

            if website:
                logger.info("  Found: %s", website)
            else:
                logger.info("  No website found")

            # Write in batches of 25 to avoid losing progress
            if (i + 1) % 25 == 0 or i == total - 1:
                _write_column(ws, website_col_idx, results)
                logger.info("  [Checkpoint] Wrote %d website values to sheet", i + 1)

            await asyncio.sleep(PAGE_DELAY_S)

        await browser.close()

    # Final write
    _write_column(ws, website_col_idx, results)
    found = sum(1 for w in results if w)
    logger.info("Done. Found websites for %d / %d companies.", found, total)


def _write_column(ws: gspread.Worksheet, col_idx: int, values: list[str]) -> None:
    """Write a list of values into a column (starting at row 2, 0-based col_idx)."""
    col_letter = _col_letter(col_idx + 1)
    cell_range = f"{col_letter}2:{col_letter}{len(values) + 1}"
    ws.update(values=[[v] for v in values], range_name=cell_range, value_input_option="USER_ENTERED")


def _col_letter(n: int) -> str:
    """Convert 1-based column number to A1-notation letter(s)."""
    result = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result


if __name__ == "__main__":
    asyncio.run(run())
