"""Hunter.io email enrichment module.

Reads officer records (ProPublica) and company lists (CDP, B Corp),
looks up emails via Hunter.io, and writes results to a 'Contacts' tab.

ProPublica  → Email Finder per officer name (Officers tab)
CDP         → Domain Search per company (known domain map)
B Corp      → Domain Search per company (domain guessed from name)

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

# Known domains for CDP/B Corp brands (fragment → domain).
KNOWN_DOMAINS: dict[str, str] = {
    "l'oreal":             "loreal.com",
    "loreal":              "loreal.com",
    "unilever":            "unilever.com",
    "procter & gamble":    "pg.com",
    "procter and gamble":  "pg.com",
    "estee lauder":        "elcompanies.com",
    "beiersdorf":          "beiersdorf.com",
    "shiseido":            "shiseido.com",
    "kao corporation":     "kao.com",
    "coty":                "coty.com",
    "avon":                "avon.com",
    "amorepacific":        "amorepacific.com",
    "lvmh":                "lvmh.com",
    "interparfums":        "interparfums.com",
    "revlon":              "revlon.com",
    "elizabeth arden":     "elizabetharden.com",
    "clarins":             "clarins.com",
    "kimberly-clark":      "kimberly-clark.com",
    "kimberly clark":      "kimberly-clark.com",
    "colgate-palmolive":   "colgatepalmolive.com",
    "colgate palmolive":   "colgatepalmolive.com",
    "reckitt":             "reckitt.com",
    "johnson & johnson":   "jnj.com",
    "johnson and johnson": "jnj.com",
    "henkel":              "henkel.com",
    "patagonia":           "patagonia.com",
    "seventh generation":  "seventhgeneration.com",
    "method":              "methodproducts.com",
    "eileen fisher":       "eileenfisher.com",
    "ben & jerry":         "benjerry.com",
    "ben and jerry":       "benjerry.com",
    "prana":               "prana.com",
    "preserve":            "preserveproducts.com",
    "dr. bronner":         "drbronner.com",
    "dr bronner":          "drbronner.com",
    "burts bees":          "burtsbees.com",
    "burt's bees":         "burtsbees.com",
    "pact":                "wearpact.com",
    "allbirds":            "allbirds.com",
    "honest company":      "honest.com",
    "grove collaborative": "grove.co",
}

_LEGAL_SUFFIX_RE = re.compile(
    r"\b(inc|llc|ltd|corp|co|company|group|holdings|international|intl|plc|sa|ag|gmbh|bv|srl)\b\.?",
    re.IGNORECASE,
)

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
    "source",
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


def _domain_for(company_name: str) -> str:
    """Return a domain for a company: known map first, then slug-guess."""
    name_lower = company_name.lower()
    for fragment, domain in KNOWN_DOMAINS.items():
        if fragment in name_lower:
            return domain
    slug = re.sub(r"[^a-z0-9]", "", _LEGAL_SUFFIX_RE.sub("", name_lower))
    return f"{slug}.com" if slug else ""


def _domain_search(domain: str, api_key: str, max_results: int = 3) -> list[dict]:
    """Hunter.io Domain Search — returns up to max_results contacts for a domain."""
    params = {"domain": domain, "limit": min(max_results, 10), "api_key": api_key}
    time.sleep(random.uniform(1.5, 2.5))
    for attempt in range(2):
        try:
            resp = requests.get(f"{HUNTER_BASE}/domain-search", params=params,
                                timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                print("[hunter] Rate limit — waiting 60s", file=sys.stderr)
                time.sleep(60)
                continue
            if resp.status_code == 401:
                return [{"_error": "invalid_key"}]
            resp.raise_for_status()
            emails = resp.json().get("data", {}).get("emails", [])[:max_results]
            return [
                {
                    "officer_name": f"{e.get('first_name','')} {e.get('last_name','')}".strip(),
                    "title":         e.get("position", ""),
                    "email":         e.get("value", ""),
                    "email_confidence": str(e.get("confidence", "")),
                    "hunter_status": "found",
                }
                for e in emails if e.get("value")
            ]
        except requests.exceptions.RequestException as exc:
            print(f"[hunter] Domain search error ({domain}): {exc}", file=sys.stderr)
    return []


def _enrich_domain_tab(spreadsheet, tab_name: str, api_key: str) -> list[dict]:
    """Read every company from tab_name, run Domain Search, return contact rows."""
    try:
        ws = spreadsheet.worksheet(tab_name)
    except Exception as exc:
        print(f"[hunter] Cannot open tab '{tab_name}': {exc}", file=sys.stderr)
        return []

    rows = ws.get_all_values()
    if len(rows) < 2:
        print(f"[hunter] Tab '{tab_name}' has no data rows", file=sys.stderr)
        return []

    header = rows[0]
    name_col = header.index("company_name") if "company_name" in header else 0

    seen: set[str] = set()
    companies: list[str] = []
    for row in rows[1:]:
        name = row[name_col].strip() if name_col < len(row) else ""
        if name and name.lower() not in seen:
            seen.add(name.lower())
            companies.append(name)

    print(
        f"[hunter] {tab_name}: {len(companies)} unique companies → Domain Search "
        f"(≈{len(companies) * 2}s + API time)",
        file=sys.stderr,
    )

    contacts: list[dict] = []
    for i, name in enumerate(companies, 1):
        domain = _domain_for(name)
        if not domain:
            print(f"[hunter]   [{i}/{len(companies)}] '{name}': no domain — skipping", file=sys.stderr)
            continue

        print(f"[hunter]   [{i}/{len(companies)}] '{name}' → {domain}", file=sys.stderr)
        results = _domain_search(domain, api_key, max_results=3)

        if not results:
            continue
        if "_error" in results[0]:
            err = results[0]["_error"]
            print(f"[hunter]        error: {err}", file=sys.stderr)
            if err == "invalid_key":
                break
            continue

        for c in results:
            contacts.append({
                "source":           tab_name,
                "org_name":         name,
                "officer_name":     c.get("officer_name", ""),
                "title":            c.get("title", ""),
                "email":            c.get("email", ""),
                "email_confidence": c.get("email_confidence", ""),
                "hunter_status":    c.get("hunter_status", ""),
                "report_url":       "",
                "enriched_at":      _now_utc(),
            })
            print(
                f"[hunter]        {c.get('officer_name','?')}  <{c.get('email','')}>  "
                f"{c.get('title','—')}",
                file=sys.stderr,
            )

    found = sum(1 for c in contacts if c.get("email"))
    print(f"[hunter] {tab_name}: {found} emails across {len(companies)} companies", file=sys.stderr)
    return contacts


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
    """Clear and rewrite the Contacts tab with all enriched contacts."""
    from pipeline.sheets_sync import _get_or_create_tab

    ws = _get_or_create_tab(spreadsheet, "Contacts")
    rows = [[str(c.get(f, "")) for f in CONTACTS_FIELDS] for c in contacts]
    ws.clear()
    ws.update([CONTACTS_FIELDS] + rows, value_input_option="USER_ENTERED")
    found = sum(1 for c in contacts if c.get("email"))
    print(
        f"[hunter] 'Contacts' tab: {len(rows)} contacts, {found} with email",
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
    for c in contacts:
        c.setdefault("source", "ProPublica")
    if contacts:
        sync_contacts_tab(spreadsheet, contacts)


DOMAINS_TAB = "Company Domains"
DOMAINS_FIELDS = ["source", "company_name", "domain", "domain_source"]


_SKIP_TABS = {
    "Company Domains", "Contacts", "Top Performers Contacts",
    "Scoring Guide", "Officers",
}


def build_company_domains_tab(spreadsheet) -> None:
    """Write a 'Company Domains' tab listing every company and its resolved domain.

    Reads company_name from every data tab (Master, Opportunities, CDP, B Corp,
    ProPublica, Government Grants, etc.), deduplicates globally, resolves each
    name to a domain via KNOWN_DOMAINS or a slug-guess fallback, and writes the
    results. domain_source indicates known vs. guessed.
    """
    from pipeline.sheets_sync import _get_or_create_tab

    # Discover all worksheets and read from any that have a company_name column
    # (or whose first non-empty column looks like company names for header-less tabs).
    all_sheets = spreadsheet.worksheets()

    global_seen: set[str] = set()
    rows: list[list[str]] = []

    for ws in all_sheets:
        if ws.title in _SKIP_TABS or ws.title == DOMAINS_TAB:
            continue

        try:
            tab_rows = ws.get_all_values()
        except Exception as exc:
            print(f"[domains] Cannot read '{ws.title}': {exc}", file=sys.stderr)
            continue

        if not tab_rows:
            continue

        # Determine name column: prefer explicit 'company_name' header, else column 0
        header = tab_rows[0]
        if "company_name" in header:
            name_col = header.index("company_name")
            data_rows = tab_rows[1:]
        else:
            # Header-less tab (e.g. Master) — treat col 0 as name
            name_col = 0
            data_rows = tab_rows

        found_in_tab = 0
        for row in data_rows:
            name = row[name_col].strip() if name_col < len(row) else ""
            if not name or name.lower() in global_seen:
                continue
            global_seen.add(name.lower())

            name_lower = name.lower()
            known_hit = next(
                (domain for frag, domain in KNOWN_DOMAINS.items() if frag in name_lower),
                None,
            )
            if known_hit:
                domain, domain_src = known_hit, "known"
            else:
                slug = re.sub(r"[^a-z0-9]", "", _LEGAL_SUFFIX_RE.sub("", name_lower))
                domain, domain_src = (f"{slug}.com" if slug else ""), "guessed"

            rows.append([ws.title, name, domain, domain_src])
            found_in_tab += 1

        print(f"[domains] '{ws.title}': {found_in_tab} unique companies", file=sys.stderr)

    ws_out = _get_or_create_tab(spreadsheet, DOMAINS_TAB)
    ws_out.clear()
    ws_out.update([DOMAINS_FIELDS] + rows, value_input_option="USER_ENTERED")

    # Apply same header formatting as other tabs
    from pipeline.sheets_sync import _apply_source_tab_formatting
    _apply_source_tab_formatting(spreadsheet, ws_out)

    known_count = sum(1 for r in rows if r[3] == "known")
    print(
        f"[domains] '{DOMAINS_TAB}' tab written: {len(rows)} companies "
        f"({known_count} known domains, {len(rows) - known_count} guessed)",
        file=sys.stderr,
    )


def enrich_all_contacts(spreadsheet) -> None:
    """Enrich contacts from CDP, B Corp, and ProPublica and write to the Contacts tab.

    CDP + B Corp  → Domain Search (up to 3 contacts per company)
    ProPublica    → Email Finder via Officers tab (sustainability-title filter)

    Each source is written to the sheet immediately after it completes so partial
    results are saved even if a later source hits Hunter.io rate limits.
    """
    api_key = os.environ.get("HUNTER_API_KEY", "").strip()
    if not api_key:
        print(
            "[hunter] HUNTER_API_KEY not set — cannot look up contacts.\n"
            "         Add HUNTER_API_KEY=<key> to your .env file.",
            file=sys.stderr,
        )
        return

    all_contacts: list[dict] = []

    # ── CDP ──────────────────────────────────────────────────────────────
    print("\n[hunter] === CDP (Domain Search) ===", file=sys.stderr)
    all_contacts.extend(_enrich_domain_tab(spreadsheet, "CDP", api_key))
    sync_contacts_tab(spreadsheet, all_contacts)
    print(f"[hunter] Contacts tab updated: {len(all_contacts)} rows so far", file=sys.stderr)

    # ── B Corp ───────────────────────────────────────────────────────────
    print("\n[hunter] === B Corp (Domain Search) ===", file=sys.stderr)
    all_contacts.extend(_enrich_domain_tab(spreadsheet, "B Corp", api_key))
    sync_contacts_tab(spreadsheet, all_contacts)
    print(f"[hunter] Contacts tab updated: {len(all_contacts)} rows so far", file=sys.stderr)

    # ── ProPublica: email finder via Officers tab ─────────────────────────
    print("\n[hunter] === ProPublica (Email Finder via Officers tab) ===", file=sys.stderr)
    try:
        off_ws = spreadsheet.worksheet("Officers")
        off_rows = off_ws.get_all_values()
    except Exception as exc:
        print(f"[hunter] Cannot open Officers tab: {exc}", file=sys.stderr)
        off_rows = []

    if len(off_rows) >= 2:
        off_header = off_rows[0]
        off_map = {f: i for i, f in enumerate(off_header)}
        officer_records = [
            {f: (row[i] if i < len(row) else "") for f, i in off_map.items()}
            for row in off_rows[1:]
        ]
        pp_contacts = enrich_officers(officer_records, api_key)
        for c in pp_contacts:
            c["source"] = "ProPublica"
        all_contacts.extend(pp_contacts)
    else:
        print("[hunter] Officers tab empty or not found — skipping ProPublica", file=sys.stderr)

    sync_contacts_tab(spreadsheet, all_contacts)
    found = sum(1 for c in all_contacts if c.get("email"))
    print(
        f"\n[hunter] Done — {found}/{len(all_contacts)} contacts with email written to 'Contacts'",
        file=sys.stderr,
    )
