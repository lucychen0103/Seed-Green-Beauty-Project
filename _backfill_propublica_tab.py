"""Backfill ProPublica tab: merges every ProPublica row from Master into ProPublica tab.

Strategy: read both tabs, build the full deduplicated dataset, resize the sheet,
clear it, then rewrite everything in one update call — avoids append_rows quirks.

Master tab layout (no header row):
  Rows 1–51:    data at column offset 0 (columns A–M)
  Rows 52–end:  data at column offset 6 (columns G–S)
Column order matches the ProPublica tab header (13 cols, no normalized_score).
"""

import os
import re as _re
import sys
from pathlib import Path

_env = Path(__file__).parent / ".env"
if _env.exists():
    content = _env.read_text()
    for m in _re.finditer(r'^([A-Z_]+)=([\s\S]*?)(?=\n[A-Z_]+=|\Z)', content, _re.MULTILINE):
        k, v = m.group(1), m.group(2).strip()
        if k not in os.environ:
            os.environ[k] = v

from pipeline.sheets_sync import get_client, _open_spreadsheet, _get_or_create_tab

PP_HEADER = [
    "company_name", "source", "disclosure_status", "score_or_rating",
    "sector", "year_of_disclosure", "report_url", "funding_type",
    "beauty_alignment", "sustainability_keywords", "scraped_at", "notes", "officers",
]
NOTES_IDX = PP_HEADER.index("notes")   # 11
SOURCE_IDX = PP_HEADER.index("source") # 1

def _ein(notes: str) -> str:
    return notes.split("|")[0].replace("EIN:", "").strip() if notes else ""

def _extract(raw: list, offset: int) -> list:
    data = raw[offset:offset + 13]
    data += [""] * (13 - len(data))
    return data

def main():
    client = get_client()
    spreadsheet = _open_spreadsheet(client)

    # ── Read Master ────────────────────────────────────────────────────────
    master_ws = spreadsheet.worksheet("Master")
    master_rows = master_ws.get_all_values()
    print(f"Master: {len(master_rows)} rows")

    # ── Read existing ProPublica tab ───────────────────────────────────────
    pp_ws = _get_or_create_tab(spreadsheet, "ProPublica")
    existing = pp_ws.get_all_values()
    print(f"ProPublica tab: {len(existing)} rows (incl header), row_count={pp_ws.row_count}")

    # Collect existing rows (skip header) keyed by EIN
    existing_by_ein: dict[str, list] = {}
    for row in (existing[1:] if existing else []):
        notes = row[NOTES_IDX] if NOTES_IDX < len(row) else ""
        e = _ein(notes)
        if e and e not in existing_by_ein:
            existing_by_ein[e] = row

    print(f"Existing unique EINs in ProPublica tab: {len(existing_by_ein)}")

    # ── Extract all ProPublica rows from Master ────────────────────────────
    added = 0
    skipped_dup = 0
    skipped_other = 0

    for raw in master_rows:
        offset = 0 if (raw and raw[0].strip()) else 6
        data = _extract(raw, offset)
        if data[SOURCE_IDX] != "propublica":
            skipped_other += 1
            continue
        e = _ein(data[NOTES_IDX])
        if e in existing_by_ein:
            skipped_dup += 1
            continue
        existing_by_ein[e] = data
        added += 1

    print(f"New rows from Master: {added} | duplicates skipped: {skipped_dup} | non-propublica: {skipped_other}")

    if added == 0:
        print("Nothing to add — ProPublica tab is already complete.")
        return

    # ── Build full dataset: header + all rows ─────────────────────────────
    all_rows = [PP_HEADER] + list(existing_by_ein.values())
    total_rows = len(all_rows)
    print(f"Total rows to write (incl header): {total_rows}")

    # ── Resize sheet if needed ────────────────────────────────────────────
    needed = total_rows + 10  # small buffer
    if pp_ws.row_count < needed:
        pp_ws.resize(rows=needed)
        print(f"Resized ProPublica tab to {needed} rows")

    # ── Clear and rewrite in chunks of 1000 rows ──────────────────────────
    pp_ws.clear()
    CHUNK = 1000
    for start in range(0, total_rows, CHUNK):
        chunk = all_rows[start:start + CHUNK]
        end_row = start + len(chunk)
        pp_ws.update(f"A{start + 1}", chunk, value_input_option="RAW")
        print(f"  Wrote rows {start + 1}–{end_row} of {total_rows}")

    print(f"\nDone: ProPublica tab now has {total_rows - 1} data rows.")

if __name__ == "__main__":
    main()
