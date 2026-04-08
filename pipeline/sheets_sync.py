"""Google Sheets sync module.

Writes scraped records to the appropriate source tab (cleared + rewritten each run)
and appends new records to the Master tab (never cleared).

Tabs expected in the spreadsheet: Master, CDP, ProPublica, B Corp, UNGC, GRI
"""

import json
import os
import sys

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SCHEMA_FIELDS = [
    "company_name",
    "source",
    "disclosure_status",
    "score_or_rating",
    "sector",
    "year_of_disclosure",
    "report_url",
    "funding_type",
    "beauty_alignment",
    "sustainability_keywords",
    "scraped_at",
    "notes",
    "officers",  # populated by ProPublica scraper; empty for other sources
]

OFFICERS_TAB = "Officers"
OFFICERS_FIELDS = ["org_name", "ein", "officer_name", "title", "report_url", "scraped_at"]

MASTER_TAB = "Master"


def get_client() -> gspread.Client:
    """Build a gspread client from the GOOGLE_SERVICE_ACCOUNT_JSON env var."""
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise EnvironmentError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not set. "
            "Add the service account JSON contents to your .env file or GitHub Secrets."
        )
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EnvironmentError(
            f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}"
        ) from exc

    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _open_spreadsheet(client: gspread.Client) -> gspread.Spreadsheet:
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise EnvironmentError(
            "SPREADSHEET_ID is not set. "
            "Add the Google Sheets spreadsheet ID to your .env file or GitHub Secrets."
        )
    return client.open_by_key(spreadsheet_id)


def _records_to_rows(records: list[dict]) -> list[list]:
    """Convert records to row lists using SCHEMA_FIELDS order."""
    rows = []
    for rec in records:
        row = []
        for field in SCHEMA_FIELDS:
            val = rec.get(field, "")
            if isinstance(val, list):
                val = ", ".join(str(v) for v in val)
            elif val is None:
                val = ""
            else:
                val = str(val)
            row.append(val)
        rows.append(row)
    return rows


def _get_or_create_tab(spreadsheet: gspread.Spreadsheet, tab_name: str) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        print(f"[sheets] Tab '{tab_name}' not found — creating it", file=sys.stderr)
        return spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=len(SCHEMA_FIELDS))


def overwrite_tab(spreadsheet: gspread.Spreadsheet, tab_name: str, records: list[dict]) -> None:
    """Clear the tab and rewrite with header + all records."""
    ws = _get_or_create_tab(spreadsheet, tab_name)
    ws.clear()
    header = SCHEMA_FIELDS
    rows = _records_to_rows(records)
    ws.update([header] + rows)
    print(f"[sheets] '{tab_name}' tab: wrote {len(records)} records", file=sys.stderr)


def append_to_master(spreadsheet: gspread.Spreadsheet, records: list[dict]) -> None:
    """Append only new records to Master tab. Never clears Master."""
    ws = _get_or_create_tab(spreadsheet, MASTER_TAB)
    existing = ws.get_all_values()

    if not existing:
        # Master is empty — write header first
        ws.update([SCHEMA_FIELDS])
        existing_keys: set[tuple] = set()
    else:
        existing_keys = _extract_existing_keys(existing)

    new_rows = []
    for rec in records:
        key = _dedup_key(rec)
        if key not in existing_keys:
            existing_keys.add(key)
            new_rows.append(_records_to_rows([rec])[0])

    if new_rows:
        ws.append_rows(new_rows, value_input_option="RAW")
        print(f"[sheets] Master: appended {len(new_rows)} new records ({len(records) - len(new_rows)} duplicates skipped)", file=sys.stderr)
    else:
        print(f"[sheets] Master: no new records to append (all {len(records)} already present)", file=sys.stderr)


def _dedup_key(rec: dict) -> tuple:
    """Return a deduplication key for a record."""
    source = rec.get("source", "")
    if source == "propublica":
        # EIN is embedded in the notes field as "EIN: XX-XXXXXXX | ..."
        notes = rec.get("notes", "")
        ein = notes.split("|")[0].replace("EIN:", "").strip() if notes else ""
        return ("propublica", ein)
    return (rec.get("company_name", "").lower().strip(), source)


def _extract_existing_keys(rows: list[list]) -> set[tuple]:
    """Extract dedup keys from existing Master rows."""
    if not rows:
        return set()

    try:
        header = rows[0]
        name_idx = header.index("company_name")
        source_idx = header.index("source")
        notes_idx = header.index("notes") if "notes" in header else None
    except ValueError:
        # Header row malformed — treat all as existing to be safe
        return {(str(r),) for r in rows}

    keys: set[tuple] = set()
    for row in rows[1:]:
        if not row:
            continue
        source = row[source_idx] if source_idx < len(row) else ""
        if source == "propublica" and notes_idx is not None:
            notes = row[notes_idx] if notes_idx < len(row) else ""
            ein = notes.split("|")[0].replace("EIN:", "").strip() if notes else ""
            keys.add(("propublica", ein))
        else:
            name = row[name_idx].lower().strip() if name_idx < len(row) else ""
            keys.add((name, source))
    return keys


def sync_officers_tab(spreadsheet: gspread.Spreadsheet, records: list[dict]) -> None:
    """Write one row per officer to the Officers tab (cleared + rewritten each run)."""
    officer_rows = []
    for rec in records:
        raw = rec.get("_officers_raw", [])
        if not raw:
            continue
        for officer in raw:
            officer_rows.append([
                rec.get("company_name", ""),
                rec.get("notes", "").split("|")[0].replace("EIN:", "").strip(),
                officer.get("name", ""),
                officer.get("title", ""),
                rec.get("report_url", ""),
                rec.get("scraped_at", ""),
            ])

    ws = _get_or_create_tab(spreadsheet, OFFICERS_TAB)
    ws.clear()
    ws.update([OFFICERS_FIELDS] + officer_rows)
    print(f"[sheets] 'Officers' tab: wrote {len(officer_rows)} officer rows across {sum(1 for r in records if r.get('_officers_raw'))} orgs", file=sys.stderr)


def sync(records: list[dict], tab_name: str) -> None:
    """Main entry point. Overwrites source tab, appends to Master, and updates Officers tab."""
    if not records:
        print(f"[sheets] '{tab_name}': 0 records — skipping sync", file=sys.stderr)
        return

    client = get_client()
    spreadsheet = _open_spreadsheet(client)

    overwrite_tab(spreadsheet, tab_name, records)
    append_to_master(spreadsheet, records)

    # Write Officers tab and trigger Hunter.io enrichment if officer data is present
    if any(rec.get("_officers_raw") for rec in records):
        sync_officers_tab(spreadsheet, records)

        # Build flat officer list for Hunter.io enrichment
        officer_records = []
        for rec in records:
            for officer in rec.get("_officers_raw", []):
                officer_records.append({
                    "org_name": rec.get("company_name", ""),
                    "officer_name": officer.get("name", ""),
                    "title": officer.get("title", ""),
                    "report_url": rec.get("report_url", ""),
                })

        from pipeline import hunter_sync
        hunter_sync.run(spreadsheet, officer_records)
