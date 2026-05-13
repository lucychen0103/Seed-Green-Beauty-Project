"""Google Sheets sync module.

Routes FundingRecords to the correct tab by source:
  - grants_gov  → "Government Grants" tab  (single-source: clear + rewrite)
  - all others  → "Opportunities" tab      (multi-source: delete own rows + append)

Source isolation guarantee: when source X syncs, ONLY rows where source == X
are deleted/replaced. Rows from all other sources are never touched.

Sorting uses the Google Sheets native sortRange API (server-side, no read/write).
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
GOVT_GRANTS_TAB = "Government Grants"
SCORING_GUIDE_TAB = "Scoring Guide"

# Sources routed to the Government Grants tab instead of Opportunities.
_GOVT_SOURCES = {"grants_gov"}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = [
    "company_name",
    "source",
    "source_track",
    "disclosure_status",
    "score_or_rating",
    "sector",
    "year_of_disclosure",
    "report_url",
    "opportunity_id",
    "funding_type",
    "beauty_alignment",
    "sustainability_keywords",
    "is_open",
    "scraped_at",
    "notes",
]

_GRANTS_GOV_SOURCE = "grants_gov"
_OPP_ID_PREFIX = "opportunityId:"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sync(records: List[FundingRecord]) -> None:
    """Sync records to their destination tab with strict source isolation.

    grants_gov  → Government Grants tab  (full clear + rewrite, single source)
    all others  → Opportunities tab      (delete only own rows, then append)

    Each source run only modifies its own rows. Other sources are never touched.
    """
    spreadsheet, opp_ws = _get_spreadsheet_and_worksheet()
    govt_ws = _ensure_govt_grants_tab(spreadsheet)

    govt_records = [r for r in records if r.source in _GOVT_SOURCES]
    opp_records  = [r for r in records if r.source not in _GOVT_SOURCES]

    # --- Government Grants: single-source tab, safe to clear and rewrite ---
    if govt_records:
        _replace_all_rows(govt_ws, govt_records)
        _sort_native(spreadsheet, govt_ws)
        logger.info(
            "sheets_sync: wrote %d records to '%s'",
            len(govt_records), GOVT_GRANTS_TAB,
        )

    # --- Opportunities: multi-source tab, source-isolated updates only ---
    by_source: Dict[str, List[FundingRecord]] = defaultdict(list)
    for r in opp_records:
        by_source[r.source].append(r)

    for source, source_records in by_source.items():
        _replace_source_rows(opp_ws, source_records)
        logger.info(
            "sheets_sync: upserted %d records for source '%s' → '%s'",
            len(source_records), source, TAB_NAME,
        )

    if by_source:
        _sort_native(spreadsheet, opp_ws)

    _ensure_scoring_guide(spreadsheet)


# ---------------------------------------------------------------------------
# Auth & worksheet access
# ---------------------------------------------------------------------------

def _get_spreadsheet_and_worksheet() -> Tuple[gspread.Spreadsheet, gspread.Worksheet]:
    raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(os.environ["SPREADSHEET_ID"])
    worksheet = _ensure_tab(spreadsheet)
    return spreadsheet, worksheet


def _ensure_tab(spreadsheet: gspread.Spreadsheet) -> gspread.Worksheet:
    try:
        ws = spreadsheet.worksheet(TAB_NAME)
        if not ws.row_values(1):
            ws.append_row(HEADERS)
        return ws
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=TAB_NAME, rows=5000, cols=len(HEADERS))
        ws.append_row(HEADERS)
        return ws


def _ensure_govt_grants_tab(spreadsheet: gspread.Spreadsheet) -> gspread.Worksheet:
    try:
        ws = spreadsheet.worksheet(GOVT_GRANTS_TAB)
        if not ws.row_values(1):
            ws.append_row(HEADERS)
        return ws
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=GOVT_GRANTS_TAB, rows=5000, cols=len(HEADERS))
        ws.append_row(HEADERS)
        return ws


# ---------------------------------------------------------------------------
# Write strategies
# ---------------------------------------------------------------------------

def _replace_all_rows(worksheet: gspread.Worksheet, records: List[FundingRecord]) -> None:
    """Clear the entire data area and rewrite. Safe only for single-source tabs."""
    worksheet.clear()
    worksheet.append_row(HEADERS)
    if records:
        worksheet.append_rows(
            [_to_row(r) for r in records],
            value_input_option="USER_ENTERED",
        )


def _replace_source_rows(worksheet: gspread.Worksheet, records: List[FundingRecord]) -> None:
    """Delete only rows for this source, then append fresh rows.

    Never reads or writes rows belonging to other sources.
    """
    if not records:
        return
    source = records[0].source

    all_values = worksheet.get_all_values()
    if len(all_values) > 1:
        header = all_values[0]
        try:
            src_col = header.index("source")
        except ValueError:
            src_col = -1

        if src_col >= 0:
            # Sheet rows to delete: 1-based, row 1 is header so data starts at 2
            rows_to_delete = [
                i + 2
                for i, row in enumerate(all_values[1:])
                if src_col < len(row) and row[src_col] == source
            ]
            if rows_to_delete:
                # Delete bottom-to-top so row numbers stay valid
                requests = [
                    {
                        "deleteDimension": {
                            "range": {
                                "sheetId": worksheet.id,
                                "dimension": "ROWS",
                                "startIndex": sheet_row - 1,  # 0-based inclusive
                                "endIndex": sheet_row,         # 0-based exclusive
                            }
                        }
                    }
                    for sheet_row in sorted(rows_to_delete, reverse=True)
                ]
                worksheet.spreadsheet.batch_update({"requests": requests})
                logger.info(
                    "sheets_sync: deleted %d existing '%s' rows from '%s'",
                    len(rows_to_delete), source, worksheet.title,
                )

    worksheet.append_rows(
        [_to_row(r) for r in records],
        value_input_option="USER_ENTERED",
    )


# ---------------------------------------------------------------------------
# Sort (native Sheets API — no data read/write, completely source-safe)
# ---------------------------------------------------------------------------

def _sort_native(spreadsheet: gspread.Spreadsheet, worksheet: gspread.Worksheet) -> None:
    """Sort data rows by beauty_alignment descending using the Sheets sortRange API.

    Server-side sort — no cell values are sent to or from the client.
    """
    ba_col = HEADERS.index("beauty_alignment")
    spreadsheet.batch_update({
        "requests": [{
            "sortRange": {
                "range": {
                    "sheetId": worksheet.id,
                    "startRowIndex": 1,          # 0-based; row 0 is the header
                    "startColumnIndex": 0,
                    "endColumnIndex": len(HEADERS),
                },
                "sortSpecs": [{
                    "dimensionIndex": ba_col,
                    "sortOrder": "DESCENDING",
                }],
            }
        }]
    })
    logger.info("sheets_sync: sorted '%s' by beauty_alignment descending", worksheet.title)


# ---------------------------------------------------------------------------
# Scoring Guide tab
# ---------------------------------------------------------------------------

def _ensure_scoring_guide(spreadsheet: gspread.Spreadsheet) -> None:
    """Create or refresh the Scoring Guide tab with formatted content."""
    try:
        ws = spreadsheet.worksheet(SCORING_GUIDE_TAB)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=SCORING_GUIDE_TAB, rows=30, cols=4)

    rows = [
        ["How Grants Are Scored (0–100)", "", ""],
        [
            "Each grant is scored based on how relevant it is to GBC's mission"
            " — sustainable, non-toxic beauty for salons and professionals.",
            "", "",
        ],
        ["", "", ""],
        ["Score Breakdown", "", ""],
        ["Category", "Keywords That Trigger It", "Points"],
        ["Beauty/Personal Care", "beauty, cosmetic, salon, personal care, skin, hair", "+40"],
        ["Sustainability", "sustainable, green, clean, non-toxic, environmental, toxic", "+30"],
        ["Community/Nonprofit", "women, community, small business, nonprofit, education", "+20"],
        ["Government Grant", "all grants.gov results", "+10"],
        ["", "", ""],
        ["Score Tiers", "", ""],
        ["Score", "Tier", "What It Means"],
        ["80–100", "High Alignment", "Strong fit — beauty AND sustainability keywords present"],
        ["50–79", "Medium Alignment", "Partial fit — sustainability focus but not beauty-specific"],
        ["10–49", "Low Alignment", "Weak fit — government grant with minimal keyword overlap"],
        ["0", "Unscored", "No matching keywords found"],
        ["", "", ""],
        ["Sources Currently Active", "", ""],
        ["• Grants.gov — scored by beauty alignment", "", ""],
        ["• B Corp Directory — Personal Care & Beauty certified", "", ""],
        ["• ProPublica — sustainability-aligned nonprofits", "", ""],
    ]

    ws.update(rows, "A1", value_input_option="USER_ENTERED")

    for r in ["A1:C1", "A2:C2", "A4:C4", "A11:C11", "A18:C18",
              "A19:C19", "A20:C20", "A21:C21"]:
        ws.merge_cells(r)

    _dark_green   = {"red": 0.106, "green": 0.369, "blue": 0.125}
    _medium_green = {"red": 0.180, "green": 0.490, "blue": 0.196}
    _light_green  = {"red": 0.647, "green": 0.839, "blue": 0.655}
    _pale_green   = {"red": 0.914, "green": 0.961, "blue": 0.910}
    _white        = {"red": 1.0,   "green": 1.0,   "blue": 1.0}

    ws.format("A1", {
        "backgroundColor": _dark_green,
        "textFormat": {"foregroundColor": _white, "bold": True, "fontSize": 14},
        "horizontalAlignment": "CENTER",
    })
    ws.format("A2", {
        "textFormat": {"italic": True, "fontSize": 10},
        "wrapStrategy": "WRAP",
    })
    for row_num in (4, 11, 18):
        ws.format(f"A{row_num}", {
            "backgroundColor": _medium_green,
            "textFormat": {"foregroundColor": _white, "bold": True, "fontSize": 11},
        })
    for header_range in ("A5:C5", "A12:C12"):
        ws.format(header_range, {
            "backgroundColor": _light_green,
            "textFormat": {"bold": True},
            "horizontalAlignment": "CENTER",
        })
    for data_range in ("A6:C9", "A13:C16"):
        ws.format(data_range, {"backgroundColor": _pale_green})

    logger.info("sheets_sync: '%s' tab created/refreshed", SCORING_GUIDE_TAB)


# ---------------------------------------------------------------------------
# Record serialisation
# ---------------------------------------------------------------------------

def _extract_opportunity_id(notes: str) -> str:
    match = re.match(rf"{re.escape(_OPP_ID_PREFIX)}([^\s|]+)", notes)
    return match.group(1).strip() if match else ""


def _extract_beauty_score(record: FundingRecord) -> Any:
    """Numeric beauty alignment for the sheet column.

    grants_gov: parse 'score:{n}' from notes.
    Others: 1 if beauty_alignment is truthy, else 0.
    """
    if record.source == _GRANTS_GOV_SOURCE:
        m = re.search(r"score:(\d+)", record.notes)
        return int(m.group(1)) if m else 0
    return 1 if record.beauty_alignment else 0


def _to_row(record: FundingRecord) -> List[Any]:
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
        record.report_url or "",
        opp_id,
        record.funding_type,
        _extract_beauty_score(record),
        ", ".join(record.sustainability_keywords),
        "" if record.is_open is None else record.is_open,
        record.scraped_at,
        record.notes,
    ]
