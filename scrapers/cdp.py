"""CDP scraper — Milestone 1.

Navigates to data.cdp.net, downloads the latest corporate disclosure CSV,
filters for beauty/personal care sector companies, and returns records
conforming to the unified schema.
"""

import asyncio
import csv
import io
import random
import sys
import time
from datetime import datetime, timezone

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

BASE_URL = "https://www.cdp.net/en/responses"
DOWNLOAD_URL = "https://www.cdp.net/en/responses?queries%5Bname%5D=&queries%5Byear%5D%5B%5D=2023&queries%5Bsector%5D%5B%5D=Personal+Care&utf8=%E2%9C%93"

BEAUTY_KEYWORDS = ("personal care", "beauty", "cosmetics", "fragrance", "skincare")

SCHEMA_DEFAULTS = {
    "source": "cdp",
    "disclosure_status": True,
    "funding_type": "corporate_sponsor",
    "beauty_alignment": True,
    "sustainability_keywords": ["carbon disclosure", "CDP signatory"],
    "notes": "",
}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_beauty(row: dict) -> bool:
    sector = (row.get("Sector") or row.get("sector") or "").lower()
    primary = (row.get("Primary activity") or row.get("primary_activity") or "").lower()
    return any(kw in sector or kw in primary for kw in BEAUTY_KEYWORDS)


def _map_row(row: dict) -> dict:
    """Map a CDP CSV row to the unified schema."""
    return {
        "company_name": row.get("Organization") or row.get("organization") or row.get("name") or "",
        "source": "cdp",
        "disclosure_status": True,
        "score_or_rating": row.get("Score") or row.get("score") or "",
        "sector": row.get("Sector") or row.get("sector") or "",
        "year_of_disclosure": _parse_year(row.get("Year") or row.get("year") or ""),
        "report_url": row.get("Report URL") or row.get("report_url") or "",
        "funding_type": "corporate_sponsor",
        "beauty_alignment": True,
        "sustainability_keywords": ["carbon disclosure", "CDP signatory"],
        "scraped_at": _now_utc(),
        "notes": "",
    }


def _parse_year(val: str):
    try:
        return int(str(val).strip()[:4])
    except (ValueError, TypeError):
        return None


async def _download_csv_with_retry(page, url: str, max_retries: int = 3) -> "str | None":
    """Navigate to URL and capture CSV download, with exponential backoff retries."""
    for attempt in range(max_retries):
        try:
            async with page.expect_download(timeout=60_000) as download_info:
                await page.goto(url, wait_until="networkidle", timeout=60_000)
                # Try clicking a CSV export/download button if present
                try:
                    await page.click("text=Download", timeout=5_000)
                except Exception:
                    pass
            download = await download_info.value
            content = await download.path()
            with open(content, "r", encoding="utf-8-sig") as f:
                return f.read()
        except PlaywrightTimeoutError as exc:
            print(f"[cdp] Attempt {attempt + 1} timed out: {exc}", file=sys.stderr)
        except Exception as exc:
            print(f"[cdp] Attempt {attempt + 1} failed: {exc}", file=sys.stderr)
        if attempt < max_retries - 1:
            sleep_secs = 2 ** (attempt + 1)
            print(f"[cdp] Retrying in {sleep_secs}s...", file=sys.stderr)
            await asyncio.sleep(sleep_secs)
    return None


async def _scrape_via_search(page) -> list[dict]:
    """
    Fallback: paginate the CDP search results page and scrape table rows
    when direct CSV download is unavailable.
    """
    records: list[dict] = []
    page_num = 1

    while True:
        url = (
            f"https://www.cdp.net/en/responses"
            f"?queries%5Bname%5D=&queries%5Bsector%5D%5B%5D=Personal+Care"
            f"&page={page_num}"
        )
        for attempt in range(3):
            try:
                await page.goto(url, wait_until="networkidle", timeout=60_000)
                break
            except PlaywrightTimeoutError as exc:
                if attempt == 2:
                    print(f"[cdp] Page {page_num} failed after 3 attempts: {exc}", file=sys.stderr)
                    return records
                await asyncio.sleep(2 ** (attempt + 1))

        rows = await page.query_selector_all("table tbody tr")
        if not rows:
            break

        for row in rows:
            cells = await row.query_selector_all("td")
            texts = [await c.inner_text() for c in cells]
            if not texts:
                continue
            record = {
                "company_name": texts[0].strip() if len(texts) > 0 else "",
                "source": "cdp",
                "disclosure_status": True,
                "score_or_rating": texts[2].strip() if len(texts) > 2 else "",
                "sector": texts[1].strip() if len(texts) > 1 else "",
                "year_of_disclosure": _parse_year(texts[3]) if len(texts) > 3 else None,
                "report_url": "",
                "funding_type": "corporate_sponsor",
                "beauty_alignment": True,
                "sustainability_keywords": ["carbon disclosure", "CDP signatory"],
                "scraped_at": _now_utc(),
                "notes": "",
            }
            # Try to get report URL from link
            link = await row.query_selector("a")
            if link:
                href = await link.get_attribute("href")
                if href:
                    record["report_url"] = href if href.startswith("http") else f"https://www.cdp.net{href}"

            records.append(record)

        # Check for next page
        next_btn = await page.query_selector("a[rel='next'], .pagination .next a")
        if not next_btn:
            break

        delay = random.uniform(2, 5)
        print(f"[cdp] Page {page_num} scraped ({len(rows)} rows). Sleeping {delay:.1f}s...", file=sys.stderr)
        await asyncio.sleep(delay)
        page_num += 1

    return records


async def run() -> list[dict]:
    """Entry point. Returns unified schema records for beauty-sector CDP disclosures."""
    records: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        try:
            # Attempt 1: direct CSV download
            csv_text = await _download_csv_with_retry(page, DOWNLOAD_URL)
            if csv_text:
                reader = csv.DictReader(io.StringIO(csv_text))
                for row in reader:
                    if _is_beauty(row):
                        records.append(_map_row(row))
                print(f"[cdp] CSV download: {len(records)} beauty-sector records", file=sys.stderr)
            else:
                # Attempt 2: scrape search results pages
                print("[cdp] CSV download unavailable — falling back to page scrape", file=sys.stderr)
                records = await _scrape_via_search(page)
                print(f"[cdp] Page scrape: {len(records)} records", file=sys.stderr)
        except Exception as exc:
            print(f"[cdp] Unexpected error: {exc}", file=sys.stderr)
        finally:
            await browser.close()

    if not records:
        print("[cdp] WARNING: 0 records returned — skipping Sheets sync for this source", file=sys.stderr)

    return records
