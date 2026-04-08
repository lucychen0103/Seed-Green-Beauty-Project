"""Google Sheets sync module.

Upserts FundingRecord lists into the 'Opportunities' tab of the configured
spreadsheet. gspread has no native upsert — this module loads existing rows,
builds a keyed index, then updates in-place or appends. Upsert key varies
by source; see _key_fields() for the full mapping.
"""

import json
import logging
import os
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

from scrapers.base import FundingRecord

load_dotenv()

logger = logging.getLogger(__name__)

TAB_NAME = "Opportunities"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Column order in the sheet — must match HEADERS exactly.
HEADERS = [
    "company_name",
    "source",
    "source_track",
    "disclosure_status",
    "score_or_rating",
    "sector",
    "year_of_disclosure",
    "report_url",
    "opportunity_id",     # grants_gov only; blank for all other sources
    "funding_type",
    "beauty_alignment",
    "sustainability_keywords",
    "is_open",
    "scraped_at",
    "notes",
]

# Sources where report_url is a stable unique identifier.
_REPORT_URL_KEY_SOURCES = {
    "bcorp",
    "gri",
    "sephora_accelerate",
    "unilever_foundry",
    "loreal",
    "propublica",
}

# Sources where no single field is reliably stable — use composite key.
_COMPOSITE_KEY_SOURCES = {
    "epa_grants",
    "california_hcd",
}

_GRANTS_GOV_SOURCE = "grants_gov"
_OPP_ID_PREFIX = "opportunityId:"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sync(records: List[FundingRecord]) -> None:
    """Upsert all records into the Opportunities tab, grouped by source.

    Skips any source group with 0 records (the individual scraper already
    logged a warning).
    """
    worksheet = _get_worksheet()

    # Load existing sheet state once — avoids repeated API calls
    existing_rows, col_index = _load_existing(worksheet)

    by_source: Dict[str, List[FundingRecord]] = defaultdict(list)
    for record in records:
        by_source[record.source].append(record)

    for source, source_records in by_source.items():
        if not source_records:
            continue
        key_cols = _key_fields(source)
        _upsert_source(worksheet, source_records, existing_rows, col_index, key_cols)
        logger.info(
            "sheets_sync: upserted %d records for source '%s' (key: %s)",
            len(source_records),
            source,
            key_cols,
        )


# ---------------------------------------------------------------------------
# Auth & worksheet access
# ---------------------------------------------------------------------------

def _get_worksheet() -> gspread.Worksheet:
    raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(os.environ["SPREADSHEET_ID"])
    return _ensure_tab(spreadsheet)


def _ensure_tab(spreadsheet: gspread.Spreadsheet) -> gspread.Worksheet:
    """Return the Opportunities worksheet, creating it with headers if absent."""
    try:
        ws = spreadsheet.worksheet(TAB_NAME)
        # If the sheet exists but is empty, write headers
        if not ws.row_values(1):
            ws.append_row(HEADERS)
        return ws
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=TAB_NAME, rows=5000, cols=len(HEADERS))
        ws.append_row(HEADERS)
        return ws


# ---------------------------------------------------------------------------
# Upsert logic
# ---------------------------------------------------------------------------

def _load_existing(
    worksheet: gspread.Worksheet,
) -> Tuple[List[List[str]], Dict[str, int]]:
    """Return all data rows and a column-name → 0-based-index mapping."""
    all_values = worksheet.get_all_values()
    if not all_values:
        return [], {}

    header_row = all_values[0]
    col_index = {name: i for i, name in enumerate(header_row)}
    data_rows = all_values[1:]  # rows below header
    return data_rows, col_index


def _key_fields(source: str) -> List[str]:
    """Return the column name(s) used as the upsert key for this source.

    Mapping:
      grants_gov                                    → ["opportunity_id"]
      epa_grants, california_hcd                    → ["source", "company_name"]
      bcorp, gri, sephora_accelerate,
        unilever_foundry, loreal, propublica        → ["report_url"]
      unknown                                       → ["source", "company_name"] + warning
    """
    if source == _GRANTS_GOV_SOURCE:
        return ["opportunity_id"]
    if source in _COMPOSITE_KEY_SOURCES:
        return ["source", "company_name"]
    if source in _REPORT_URL_KEY_SOURCES:
        return ["report_url"]

    logger.warning(
        "sheets_sync: unknown source '%s' — falling back to composite key "
        "['source', 'company_name']. Add it to _key_fields() if this is intentional.",
        source,
    )
    return ["source", "company_name"]


def _upsert_source(
    worksheet: gspread.Worksheet,
    records: List[FundingRecord],
    existing_rows: List[List[str]],
    col_index: Dict[str, int],
    key_cols: List[str],
) -> None:
    """Update matching existing rows or append new ones for this source's records."""
    rows_to_append: List[List[Any]] = []
    cells_to_update: List[gspread.Cell] = []

    for record in records:
        row_values = _to_row(record)
        incoming_key = _extract_key(row_values, key_cols)

        match_idx = _find_match(existing_rows, col_index, key_cols, incoming_key)

        if match_idx is not None:
            # Update existing row in-place (sheet row = match_idx + 2: +1 header, +1 1-based)
            sheet_row = match_idx + 2
            for col_name, value in zip(HEADERS, row_values):
                if col_name in col_index:
                    col_num = col_index[col_name] + 1  # 1-based for gspread
                    cells_to_update.append(
                        gspread.Cell(sheet_row, col_num, value)
                    )
            # Keep existing_rows in sync so subsequent records in this batch
            # don't false-match against stale data
            existing_rows[match_idx] = row_values
        else:
            rows_to_append.append(row_values)
            existing_rows.append(row_values)

    if cells_to_update:
        worksheet.update_cells(cells_to_update)

    if rows_to_append:
        worksheet.append_rows(rows_to_append)


def _find_match(
    existing_rows: List[List[str]],
    col_index: Dict[str, int],
    key_cols: List[str],
    incoming_key: Tuple[str, ...],
) -> Optional[int]:
    """Return the 0-based index into existing_rows of the first matching row, or None."""
    for i, row in enumerate(existing_rows):
        existing_key = tuple(
            row[col_index[col]] if col in col_index and col_index[col] < len(row) else ""
            for col in key_cols
        )
        if existing_key == incoming_key:
            return i
    return None


def _extract_key(row_values: List[Any], key_cols: List[str]) -> Tuple[str, ...]:
    """Extract the key tuple from a row list using HEADERS as the column map."""
    header_index = {name: i for i, name in enumerate(HEADERS)}
    return tuple(
        str(row_values[header_index[col]]) if col in header_index else ""
        for col in key_cols
    )


# ---------------------------------------------------------------------------
# Record serialisation
# ---------------------------------------------------------------------------

def _extract_opportunity_id(notes: str) -> str:
    """Extract the opportunityId value from a grants_gov notes string.

    Notes format: 'opportunityId:<id> | closingDate:... | ...'
    Returns empty string if not found.
    """
    match = re.match(rf"{re.escape(_OPP_ID_PREFIX)}([^\s|]+)", notes)
    return match.group(1).strip() if match else ""


def _to_row(record: FundingRecord) -> List[Any]:
    """Serialise a FundingRecord to a list aligned with HEADERS."""
    opp_id = (
        _extract_opportunity_id(record.notes)
        if record.source == _GRANTS_GOV_SOURCE
        else ""
    )

    return [
        record.company_name,
        record.source,
        record.source_track,
        record.disclosure_status,
        record.score_or_rating,
        record.sector,
        record.year_of_disclosure if record.year_of_disclosure is not None else "",
        record.report_url,
        opp_id,
        record.funding_type,
        record.beauty_alignment,
        ", ".join(record.sustainability_keywords),
        "" if record.is_open is None else record.is_open,
        record.scraped_at,
        record.notes,
    ]
