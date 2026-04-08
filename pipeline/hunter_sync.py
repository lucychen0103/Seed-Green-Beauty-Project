"""Hunter.io email enrichment module.

Reads officer records, filters for sustainability-relevant titles,
looks up emails via Hunter.io's Email Finder API, and writes results
to a 'Contacts' tab in Google Sheets.

Runs automatically as part of the sync pipeline whenever officer data
is present. Skips gracefully if HUNTER_API_KEY is not set.

API docs: https://hunter.io/api-documentation
"""

import os
import re
import sys
import time
import random
from datetime import datetime, timezone

import requests

HUNTER_BASE = "https://api.hunter.io/v2"
REQUEST_TIMEOUT = 20

SUSTAINABILITY_TITLE_KEYWORDS = (
    "sustainability",
    "social responsibility",
    "esg",
    "community",
    "corporate responsib", # corporate responsibility
    "green",
    "impact",
    "climate",
    "environment",        # environmental, environmentalist
    "conservation",
    "carbon",
    "net zero",
    "circular",           # circular economy
    "diversity",          # DEI often paired with social responsibility
    "inclusion",
    "equity",
    "philanthrop",        # philanthropy, philanthropic
    "purpose",            # purpose-driven roles
    "ethical",
)

CONTACTS_FIELDS = [
    "org_name",
    "officer_name",
    "title",
    "email",
    "email_confidence",
    "hunter_status",
    "report_url",
    "enriched_at",
]


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_sustainability_title(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in SUSTAINABILITY_TITLE_KEYWORDS)


def _split_name(full_name: str) -> "tuple[str, str]":
    """Split 'First Last' into (first, last). Handles multi-word last names."""
    parts = full_name.strip().split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _find_email(first_name: str, last_name: str, company: str, api_key: str) -> dict:
    """
    Call Hunter.io Email Finder. Returns dict with email, confidence, status.
    Uses company name (Hunter resolves the domain internally).
    """
    params = {
        "first_name": first_name,
        "last_name": last_name,
        "company": company,
        "api_key": api_key,
    }
    for attempt in range(3):
        try:
            resp = requests.get(f"{HUNTER_BASE}/email-finder", params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                print("[hunter] Rate limited — waiting 60s", file=sys.stderr)
                time.sleep(60)
                continue
            if resp.status_code == 401:
                print("[hunter] Invalid API key — check HUNTER_API_KEY in .env", file=sys.stderr)
                return {"email": "", "confidence": "", "status": "invalid_key"}
            if resp.status_code == 404 or resp.status_code == 400:
                return {"email": "", "confidence": "", "status": "not_found"}
            resp.raise_for_status()
            data = resp.json().get("data", {})
            return {
                "email": data.get("email") or "",
                "confidence": str(data.get("score") or ""),
                "status": data.get("status") or "found",
            }
        except requests.exceptions.RequestException as exc:
            print(f"[hunter] Request error (attempt {attempt + 1}): {exc}", file=sys.stderr)
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))

    return {"email": "", "confidence": "", "status": "error"}


EMAIL_LOOKUP_LIMIT = 10  # Max Hunter.io lookups per run to preserve API credits


def enrich_officers(officer_records: list[dict], api_key: str) -> list[dict]:
    """
    Filter officers by sustainability title, look up emails via Hunter.io.
    Falls back to all officers if none match sustainability keywords.
    Caps lookups at EMAIL_LOOKUP_LIMIT to preserve Hunter.io API credits.
    Returns list of contact dicts ready for the Contacts tab.
    """
    contacts = []
    eligible = [r for r in officer_records if _is_sustainability_title(r.get("title", ""))]

    if eligible:
        print(
            f"[hunter] {len(eligible)} sustainability-relevant officers out of {len(officer_records)} total",
            file=sys.stderr,
        )
    else:
        print(
            f"[hunter] No sustainability-relevant titles found — falling back to all {len(officer_records)} officers",
            file=sys.stderr,
        )
        eligible = officer_records

    if len(eligible) > EMAIL_LOOKUP_LIMIT:
        print(
            f"[hunter] Capping lookups at {EMAIL_LOOKUP_LIMIT} (had {len(eligible)}) to preserve API credits",
            file=sys.stderr,
        )
        eligible = eligible[:EMAIL_LOOKUP_LIMIT]

    for i, record in enumerate(eligible):
        first, last = _split_name(record.get("officer_name", ""))
        if not first or not last:
            print(f"[hunter] Skipping '{record.get('officer_name')}' — can't split name", file=sys.stderr)
            continue

        company = record.get("org_name", "")
        result = _find_email(first, last, company, api_key)

        contacts.append({
            "org_name": company,
            "officer_name": record.get("officer_name", ""),
            "title": record.get("title", ""),
            "email": result["email"],
            "email_confidence": result["confidence"],
            "hunter_status": result["status"],
            "report_url": record.get("report_url", ""),
            "enriched_at": _now_utc(),
        })

        status_msg = result["email"] if result["email"] else result["status"]
        print(f"[hunter] ({i + 1}/{len(eligible)}) {record.get('officer_name')} @ {company}: {status_msg}", file=sys.stderr)

        # Polite delay between Hunter.io requests
        time.sleep(random.uniform(1.5, 3))

    found = sum(1 for c in contacts if c["email"])
    print(f"[hunter] Emails found: {found}/{len(contacts)}", file=sys.stderr)
    return contacts


def sync_contacts_tab(spreadsheet, contacts: list[dict]) -> None:
    """
    Overwrite the Contacts tab with enriched contacts.
    Existing rows for unchanged officers are preserved via dedup on (org_name, officer_name).
    """
    from pipeline.sheets_sync import _get_or_create_tab

    ws = _get_or_create_tab(spreadsheet, "Contacts")
    existing = ws.get_all_values()

    # Build map of existing contacts keyed by (org_name, officer_name)
    existing_map: dict[tuple, list] = {}
    if existing and len(existing) > 1:
        try:
            h = existing[0]
            name_i = h.index("officer_name")
            org_i = h.index("org_name")
            for row in existing[1:]:
                if len(row) > max(name_i, org_i):
                    key = (row[org_i].strip(), row[name_i].strip())
                    existing_map[key] = row
        except (ValueError, IndexError):
            pass

    # Merge: prefer new result if email found, else keep existing non-empty email
    final_rows = []
    for c in contacts:
        key = (c["org_name"].strip(), c["officer_name"].strip())
        existing_row = existing_map.get(key)
        if existing_row and not c["email"]:
            # Keep previously found email rather than overwriting with blank
            final_rows.append(existing_row)
        else:
            final_rows.append([c.get(f, "") for f in CONTACTS_FIELDS])

    ws.clear()
    ws.update([CONTACTS_FIELDS] + final_rows)
    found = sum(1 for r in final_rows if len(r) > 3 and r[3])
    print(
        f"[hunter] 'Contacts' tab: {len(final_rows)} contacts, {found} with email",
        file=sys.stderr,
    )


def run(spreadsheet, officer_records: list[dict]) -> None:
    """
    Entry point. Called from sheets_sync.sync() when officer data is present.
    Skips gracefully if HUNTER_API_KEY is not configured.
    """
    api_key = os.environ.get("HUNTER_API_KEY", "").strip()
    if not api_key:
        print(
            "[hunter] HUNTER_API_KEY not set — skipping email enrichment. "
            "Add it to .env to enable contact lookup.",
            file=sys.stderr,
        )
        return

    if not officer_records:
        print("[hunter] No officer records to enrich.", file=sys.stderr)
        return

    contacts = enrich_officers(officer_records, api_key)
    if contacts:
        sync_contacts_tab(spreadsheet, contacts)
