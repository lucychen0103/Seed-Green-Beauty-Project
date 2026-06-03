"""One-off contact enrichment capped at 3 contacts per source database.

Usage:
    python _enrich_contacts_limited.py
"""

import os
import sys
sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv(dotenv_path=".env")

from pipeline import hunter_sync
from pipeline.sheets_sync import get_spreadsheet, _get_or_create_tab

MAX_PER_SOURCE = 2


def enrich_limited():
    api_key = os.environ.get("HUNTER_API_KEY", "").strip()
    if not api_key:
        print("HUNTER_API_KEY not set — aborting", file=sys.stderr)
        return

    spreadsheet = get_spreadsheet()
    all_contacts = []

    for source_tab in ("CDP", "B Corp"):
        print(f"\n=== {source_tab} (Domain Search, max {MAX_PER_SOURCE} contacts) ===", file=sys.stderr)
        try:
            ws = spreadsheet.worksheet(source_tab)
        except Exception as exc:
            print(f"Cannot open '{source_tab}': {exc}", file=sys.stderr)
            continue

        rows = ws.get_all_values()
        if len(rows) < 2:
            continue

        header = rows[0]
        name_col = header.index("company_name") if "company_name" in header else 0

        seen = set()
        source_contacts = []
        companies_tried = 0
        for row in rows[1:]:
            # Hard cap: stop after trying MAX_PER_SOURCE companies regardless of results
            if companies_tried >= MAX_PER_SOURCE:
                break
            name = row[name_col].strip() if name_col < len(row) else ""
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())

            domain = hunter_sync._domain_for(name)
            if not domain:
                print(f"  '{name}': no domain — skipping", file=sys.stderr)
                continue

            print(f"  '{name}' → {domain}", file=sys.stderr)
            companies_tried += 1
            results = hunter_sync._domain_search(domain, api_key, max_results=3)

            if not results:
                continue
            if "_error" in results[0]:
                print(f"  error: {results[0]['_error']}", file=sys.stderr)
                break

            for c in results:
                source_contacts.append({
                    "source": source_tab,
                    "org_name": name,
                    "officer_name": c.get("officer_name", ""),
                    "title": c.get("title", ""),
                    "email": c.get("email", ""),
                    "email_confidence": c.get("email_confidence", ""),
                    "hunter_status": c.get("hunter_status", ""),
                    "report_url": "",
                    "enriched_at": hunter_sync._now_utc(),
                })
                print(f"    {c.get('officer_name','?')} <{c.get('email','')}> {c.get('title','')}", file=sys.stderr)

        print(f"  {len(source_contacts)} contacts from {source_tab}", file=sys.stderr)
        all_contacts.extend(source_contacts)

    # ProPublica via Officers tab
    print(f"\n=== ProPublica (Email Finder via Officers tab, max {MAX_PER_SOURCE}) ===", file=sys.stderr)
    try:
        off_ws = spreadsheet.worksheet("Officers")
        off_rows = off_ws.get_all_values()
    except Exception as exc:
        print(f"Cannot open Officers tab: {exc}", file=sys.stderr)
        off_rows = []

    if len(off_rows) >= 2:
        off_header = off_rows[0]
        off_map = {f: i for i, f in enumerate(off_header)}
        officer_records = [
            {f: (row[i] if i < len(row) else "") for f, i in off_map.items()}
            for row in off_rows[1:MAX_PER_SOURCE + 1]  # cap input rows
        ]
        pp_contacts = hunter_sync.enrich_officers(officer_records, api_key)
        for c in pp_contacts:
            c["source"] = "ProPublica"
        all_contacts.extend(pp_contacts[:MAX_PER_SOURCE])
    else:
        print("Officers tab empty or not found — skipping", file=sys.stderr)

    hunter_sync.sync_contacts_tab(spreadsheet, all_contacts)
    found = sum(1 for c in all_contacts if c.get("email"))
    print(f"\nDone — {found}/{len(all_contacts)} contacts with email written to 'Contacts' tab")


if __name__ == "__main__":
    enrich_limited()
