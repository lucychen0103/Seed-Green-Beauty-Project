"""Google Sheets sync module.

Routes FundingRecords to the correct tab by source:
  - grants_gov  → "Government Grants" tab  (clear + rewrite)
  - cdp         → "CDP" tab                (clear + rewrite)
  - propublica  → "ProPublica" tab         (clear + rewrite)
  - bcorp       → "B Corp" tab             (clear + rewrite)
  - all others  → "Opportunities" tab      (source-isolated: delete own rows + append)
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
from pipeline.scoring import compute_normalized_score

load_dotenv()

logger = logging.getLogger(__name__)

TAB_NAME = "Opportunities"
GOVT_GRANTS_TAB = "Government Grants"
SCORING_GUIDE_TAB = "Scoring Guide"

# Sources that get their own dedicated tab (clear + rewrite each run).
DEDICATED_TABS: Dict[str, str] = {
    "grants_gov": GOVT_GRANTS_TAB,
    "cdp":        "CDP",
    "propublica": "ProPublica",
    "bcorp":      "B Corp",
}

# Headers used for dedicated per-source tabs.
SOURCE_TAB_HEADERS = [
    "company_name",
    "source",
    "disclosure_status",
    "score_or_rating",
    "normalized_score",
    "sector",
    "year_of_disclosure",
    "report_url",
    "funding_type",
    "beauty_alignment",
    "sustainability_keywords",
    "scraped_at",
    "notes",
]

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
    """Sync records to their destination tab.

    Sources with a dedicated tab (cdp, propublica, bcorp, grants_gov) get a
    full clear + rewrite of their own tab each run.  Everything else goes to
    the Opportunities tab with source-isolated row replacement.
    """
    spreadsheet, opp_ws = _get_spreadsheet_and_worksheet()

    # Partition records by whether they have a dedicated tab.
    dedicated: Dict[str, List[FundingRecord]] = defaultdict(list)
    opp_records: List[FundingRecord] = []
    for r in records:
        if r.source in DEDICATED_TABS:
            dedicated[r.source].append(r)
        else:
            opp_records.append(r)

    # --- Dedicated per-source tabs: clear + rewrite ---
    for source, source_records in dedicated.items():
        tab_name = DEDICATED_TABS[source]
        ws = _ensure_source_tab(spreadsheet, tab_name)
        _replace_all_rows_source(ws, source_records)
        logger.info(
            "sheets_sync: wrote %d records to '%s'", len(source_records), tab_name
        )

    # --- Opportunities: multi-source tab, source-isolated updates ---
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


def _ensure_source_tab(spreadsheet: gspread.Spreadsheet, tab_name: str) -> gspread.Worksheet:
    """Return (or create) a dedicated per-source worksheet."""
    try:
        return spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=tab_name, rows=5000, cols=len(SOURCE_TAB_HEADERS))
        ws.append_row(SOURCE_TAB_HEADERS)
        return ws


def _replace_all_rows_source(worksheet: gspread.Worksheet, records: List[FundingRecord]) -> None:
    """Clear a dedicated source tab and rewrite with current records."""
    worksheet.clear()
    worksheet.append_row(SOURCE_TAB_HEADERS)
    if records:
        worksheet.append_rows(
            [_to_source_row(r) for r in records],
            value_input_option="USER_ENTERED",
        )


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
        # Delete and recreate so all existing merges/formats are fully reset.
        spreadsheet.del_worksheet(ws)
    except gspread.WorksheetNotFound:
        pass
    ws = spreadsheet.add_worksheet(title=SCORING_GUIDE_TAB, rows=32, cols=4)

    rows = [
        ["How Leads Are Scored (0–100)", "", ""],
        [
            "Each lead is scored based on how relevant it is to GBC's mission"
            " — sustainable, non-toxic beauty for salons and professionals.",
            "", "",
        ],
        ["", "", ""],
        ["Score Breakdown by Source", "", ""],
        ["Source", "Scoring Criteria", "Points"],
        ["B Corp", "Certified B Corp (sustainability credential)", "+30"],
        ["B Corp", "Personal Care & Beauty / Health & Wellness sector", "+40"],
        ["B Corp", "Cleantech / Environmental Services sector", "+20"],
        ["B Corp", "Beauty alignment keywords detected", "+10"],
        ["CDP", "Performance Band A", "+100"],
        ["CDP", "Performance Band A-", "+90"],
        ["CDP", "Performance Band B", "+75"],
        ["CDP", "Performance Band C", "+55"],
        ["CDP", "Performance Band D / fallback numeric score", "+35"],
        ["ProPublica", "Has filed 990s (active nonprofit)", "+40"],
        ["ProPublica", "Environment sector (NTEE code C*)", "+40"],
        ["ProPublica", "Sustainability keyword in org name", "+10 each (max +20)"],
        ["", "", ""],
        ["Score Tiers", "", ""],
        ["Score", "Tier", "What It Means"],
        ["80–100", "High Alignment", "Strong fit — beauty AND sustainability present"],
        ["50–79", "Medium Alignment", "Partial fit — sustainability focus, beauty-adjacent"],
        ["10–49", "Low Alignment", "Weak fit — minimal keyword overlap"],
        ["0", "Unscored", "Source not yet scored or no keywords matched"],
        ["", "", ""],
        ["Sources Currently Active", "", ""],
        ["• B Corp Directory — certified companies across beauty, cleantech & wellness sectors", "", ""],
        ["• CDP — corporate climate disclosure scores (Performance Bands A–D)", "", ""],
        ["• ProPublica — sustainability-aligned nonprofit foundations (IRS 990 data)", "", ""],
    ]

    ws.update(rows, "A1", value_input_option="RAW")

    for r in ["A1:C1", "A2:C2", "A4:C4", "A18:C18", "A19:C19",
              "A25:C25", "A26:C26", "A27:C27", "A28:C28"]:
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
    # Section headers: Score Breakdown (row 4), Score Tiers (row 18), Sources (row 25)
    for row_num in (4, 18, 25):
        ws.format(f"A{row_num}", {
            "backgroundColor": _medium_green,
            "textFormat": {"foregroundColor": _white, "bold": True, "fontSize": 11},
        })
    # Column headers for each section
    for header_range in ("A5:C5", "A19:C19"):
        ws.format(header_range, {
            "backgroundColor": _light_green,
            "textFormat": {"bold": True},
            "horizontalAlignment": "CENTER",
        })
    # Data rows — Score Breakdown (6-17) and Score Tiers (20-23)
    for data_range in ("A6:C17", "A20:C23"):
        ws.format(data_range, {"backgroundColor": _pale_green})
    # Sources rows
    for data_range in ("A26:C28",):
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


def _to_source_row(record: FundingRecord) -> List[Any]:
    """Serialise a record for a dedicated per-source tab (SOURCE_TAB_HEADERS)."""
    score = compute_normalized_score({
        "source": record.source,
        "notes": record.notes,
        "score_or_rating": record.score_or_rating,
        "sector": record.sector.lower() if record.sector else "",
        "disclosure_status": record.disclosure_status,
        "company_name": record.company_name,
        "beauty_alignment": record.beauty_alignment,
    })
    return [
        record.company_name,
        record.source,
        record.disclosure_status,
        record.score_or_rating,
        score,
        record.sector,
        record.year_of_disclosure if record.year_of_disclosure is not None else "",
        record.report_url or "",
        record.funding_type,
        _extract_beauty_score(record),
        ", ".join(record.sustainability_keywords),
        record.scraped_at,
        record.notes,
    ]


# ---------------------------------------------------------------------------
# CLI / legacy tooling (main.py commands, backfill scripts)
# ---------------------------------------------------------------------------

def refresh_scoring_guide() -> None:
    """Connect and (re)write the Scoring Guide tab only."""
    spreadsheet, _ = _get_spreadsheet_and_worksheet()
    _ensure_scoring_guide(spreadsheet)


def get_spreadsheet() -> gspread.Spreadsheet:
    """Return the configured spreadsheet (e.g. for top_performers enrichment)."""
    spreadsheet, _ = _get_spreadsheet_and_worksheet()
    return spreadsheet


def get_client() -> gspread.Client:
    """Build a gspread client from GOOGLE_SERVICE_ACCOUNT_JSON (legacy scripts)."""
    raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _open_spreadsheet(client: gspread.Client) -> gspread.Spreadsheet:
    """Open the spreadsheet by ID (legacy scripts)."""
    return client.open_by_key(os.environ["SPREADSHEET_ID"])


def _get_or_create_tab(spreadsheet: gspread.Spreadsheet, tab_name: str) -> gspread.Worksheet:
    """Return a worksheet by title, creating it if missing (legacy scripts)."""
    try:
        return spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        logger.info("sheets_sync: creating worksheet %r", tab_name)
        return spreadsheet.add_worksheet(title=tab_name, rows=2000, cols=20)
