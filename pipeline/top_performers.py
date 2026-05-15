"""Top Performers email enrichment.

For each source tab (CDP, ProPublica), identifies the top 5 companies by
normalized_score (computed on-the-fly from the sheet data), then uses
Hunter.io to find 1-3 employee contacts per company.

  CDP companies    → Hunter.io Domain Search  (one call returns multiple contacts)
  ProPublica orgs  → Hunter.io Email Finder   (officer names already in Officers tab)

Results are written to a "Top Performers Contacts" tab.
"""

import os
import re
import sys
import time
import random
from datetime import datetime, timezone

import requests

from pipeline.scoring import compute_normalized_score

HUNTER_BASE = "https://api.hunter.io/v2"
REQUEST_TIMEOUT = 20

OUTPUT_TAB = "Top Performers Contacts"

OUTPUT_FIELDS = [
    "source",
    "rank",
    "company_name",
    "normalized_score",
    "contact_name",
    "contact_title",
    "email",
    "email_confidence",
    "hunter_status",
    "found_at",
]

# Known domains for CDP beauty/personal-care brands.
# Keyed by lowercase fragment that must appear in the company name.
KNOWN_DOMAINS: dict[str, str] = {
    "l'oreal":            "loreal.com",
    "loreal":             "loreal.com",
    "unilever":           "unilever.com",
    "procter & gamble":   "pg.com",
    "procter and gamble": "pg.com",
    "estee lauder":       "elcompanies.com",
    "beiersdorf":         "beiersdorf.com",
    "shiseido":           "shiseido.com",
    "kao corporation":    "kao.com",
    "coty":               "coty.com",
    "avon":               "avon.com",
    "amorepacific":       "amorepacific.com",
    "lvmh":               "lvmh.com",
    "interparfums":       "interparfums.com",
    "revlon":             "revlon.com",
    "elizabeth arden":    "elizabetharden.com",
    "clarins":            "clarins.com",
    "kimberly-clark":     "kimberly-clark.com",
    "kimberly clark":     "kimberly-clark.com",
    "colgate-palmolive":  "colgatepalmolive.com",
    "colgate palmolive":  "colgatepalmolive.com",
    "reckitt":            "reckitt.com",
    "johnson & johnson":  "jnj.com",
    "johnson and johnson":"jnj.com",
    "henkel":             "henkel.com",
}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Hunter.io helpers ─────────────────────────────────────────────────────────

def check_credits(api_key: str) -> dict:
    """Return Hunter.io account search-credit info."""
    try:
        resp = requests.get(f"{HUNTER_BASE}/account", params={"api_key": api_key},
                            timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        searches = resp.json().get("data", {}).get("requests", {}).get("searches", {})
        return {
            "used":      searches.get("used", "?"),
            "available": searches.get("available", "?"),
        }
    except Exception as exc:
        return {"error": str(exc)}


def _polite_sleep():
    time.sleep(random.uniform(1.5, 2.5))


def _domain_for(company_name: str) -> str:
    """Return the known domain for a company name, or empty string if unknown."""
    name_lower = company_name.lower()
    for fragment, domain in KNOWN_DOMAINS.items():
        if fragment in name_lower:
            return domain
    return ""


def domain_search(domain: str, api_key: str, max_results: int = 3) -> list[dict]:
    """
    Hunter.io Domain Search — returns up to max_results contacts for a domain.
    One API call, multiple contacts returned.
    """
    params = {"domain": domain, "limit": min(max_results, 10), "api_key": api_key}
    _polite_sleep()
    for attempt in range(2):
        try:
            resp = requests.get(f"{HUNTER_BASE}/domain-search", params=params,
                                timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                print("[top5] Rate limit hit — waiting 60s", file=sys.stderr)
                time.sleep(60)
                continue
            if resp.status_code == 401:
                return [{"_error": "invalid_key"}]
            resp.raise_for_status()
            emails = resp.json().get("data", {}).get("emails", [])[:max_results]
            return [
                {
                    "contact_name":  f"{e.get('first_name','')} {e.get('last_name','')}".strip(),
                    "contact_title": e.get("position", ""),
                    "email":         e.get("value", ""),
                    "confidence":    str(e.get("confidence", "")),
                    "status":        "found",
                }
                for e in emails if e.get("value")
            ]
        except requests.exceptions.RequestException as exc:
            print(f"[top5] Domain search error ({domain}): {exc}", file=sys.stderr)
    return []


def email_finder(first: str, last: str, company: str, api_key: str) -> dict:
    """
    Hunter.io Email Finder — looks up a specific person at a company.
    One API call per person.
    """
    params = {"first_name": first, "last_name": last, "company": company, "api_key": api_key}
    _polite_sleep()
    for attempt in range(2):
        try:
            resp = requests.get(f"{HUNTER_BASE}/email-finder", params=params,
                                timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                print("[top5] Rate limit hit — waiting 60s", file=sys.stderr)
                time.sleep(60)
                continue
            if resp.status_code in (400, 404):
                return {"email": "", "confidence": "", "status": "not_found"}
            if resp.status_code == 401:
                return {"email": "", "confidence": "", "status": "invalid_key"}
            resp.raise_for_status()
            data = resp.json().get("data", {})
            return {
                "email":      data.get("email") or "",
                "confidence": str(data.get("score") or ""),
                "status":     data.get("status") or "found",
            }
        except requests.exceptions.RequestException as exc:
            print(f"[top5] Email finder error ({first} {last}): {exc}", file=sys.stderr)
    return {"email": "", "confidence": "", "status": "error"}


# ── Sheet helpers ─────────────────────────────────────────────────────────────

def get_top_n(spreadsheet, tab_name: str, n: int = 5) -> list[dict]:
    """
    Read a data tab, score each row using compute_normalized_score,
    and return the top N unique companies (highest score wins on dedup).
    """
    try:
        ws = spreadsheet.worksheet(tab_name)
    except Exception as exc:
        print(f"[top5] Cannot open tab '{tab_name}': {exc}", file=sys.stderr)
        return []

    rows = ws.get_all_values()
    if len(rows) < 2:
        print(f"[top5] Tab '{tab_name}' has no data rows", file=sys.stderr)
        return []

    header = rows[0]
    field_map = {f: i for i, f in enumerate(header)}

    seen: dict[str, dict] = {}
    for row in rows[1:]:
        rec = {f: (row[i] if i < len(row) else "") for f, i in field_map.items()}
        score = compute_normalized_score(rec)
        name_key = rec.get("company_name", "").lower().strip()
        if not name_key:
            continue
        if name_key not in seen or score > seen[name_key]["_score"]:
            rec["_score"] = score
            seen[name_key] = rec

    top = sorted(seen.values(), key=lambda r: r["_score"], reverse=True)[:n]

    print(f"[top5] Top {len(top)} from '{tab_name}':", file=sys.stderr)
    for i, r in enumerate(top, 1):
        print(f"  {i}. {r['company_name']}  (normalized_score={r['_score']})", file=sys.stderr)
    return top


def get_officers(spreadsheet, org_name: str, max_officers: int = 3) -> list[dict]:
    """Read the Officers tab and return up to max_officers for the given org."""
    try:
        ws = spreadsheet.worksheet("Officers")
        rows = ws.get_all_values()
    except Exception:
        return []
    if len(rows) < 2:
        return []

    header = rows[0]
    try:
        org_i     = header.index("org_name")
        officer_i = header.index("officer_name")
        title_i   = header.index("title")
    except ValueError:
        return []

    results = []
    for row in rows[1:]:
        if len(row) <= max(org_i, officer_i, title_i):
            continue
        if row[org_i].lower().strip() == org_name.lower().strip():
            results.append({"name": row[officer_i], "title": row[title_i]})
        if len(results) >= max_officers:
            break
    return results


# ── Main entry point ──────────────────────────────────────────────────────────

def run(spreadsheet) -> None:
    """
    Find top 5 companies per source, enrich with Hunter.io contacts,
    and write to the Top Performers Contacts tab.
    """
    api_key = os.environ.get("HUNTER_API_KEY", "").strip()
    if not api_key:
        print(
            "[top5] HUNTER_API_KEY not set — cannot look up contacts.\n"
            "       Add HUNTER_API_KEY=<your key> to your .env file.",
            file=sys.stderr,
        )
        return

    # ── Credit check ─────────────────────────────────────────────────────
    credits = check_credits(api_key)
    if "error" in credits:
        print(f"[top5] Could not check Hunter.io credits: {credits['error']}", file=sys.stderr)
    else:
        avail = credits["available"]
        used  = credits["used"]
        print(f"[top5] Hunter.io: {used} searches used / {avail} available this month",
              file=sys.stderr)
        try:
            if int(avail) < 10:
                print(
                    f"[top5] WARNING: only {avail} searches remaining — "
                    "results may be cut short if credits run out mid-run.",
                    file=sys.stderr,
                )
        except (ValueError, TypeError):
            pass

    all_contacts: list[dict] = []

    # ── CDP: one domain-search call per company (returns 1-3 contacts) ───
    print("\n[top5] === CDP top 5 ===", file=sys.stderr)
    for rank, company in enumerate(get_top_n(spreadsheet, "CDP", n=5), 1):
        name  = company["company_name"]
        score = company["_score"]
        domain = _domain_for(name)

        if not domain:
            print(f"[top5]   #{rank} '{name}': domain unknown — skipping Hunter lookup",
                  file=sys.stderr)
            all_contacts.append(_blank_row("CDP", rank, name, score, "domain_unknown"))
            continue

        print(f"[top5]   #{rank} '{name}'  →  domain search: {domain}", file=sys.stderr)
        results = domain_search(domain, api_key, max_results=3)

        if not results:
            print(f"[top5]        no results returned", file=sys.stderr)
            all_contacts.append(_blank_row("CDP", rank, name, score, "no_results"))
            continue

        for contact in results:
            if "_error" in contact:
                print(f"[top5]        Hunter error: {contact['_error']}", file=sys.stderr)
                all_contacts.append(_blank_row("CDP", rank, name, score, contact["_error"]))
                break
            print(f"[top5]        {contact['contact_name'] or '?'}  <{contact['email']}>  "
                  f"title={contact['contact_title'] or '—'}  conf={contact['confidence']}",
                  file=sys.stderr)
            all_contacts.append({
                "source":           "CDP",
                "rank":             rank,
                "company_name":     name,
                "normalized_score": score,
                "contact_name":     contact["contact_name"],
                "contact_title":    contact["contact_title"],
                "email":            contact["email"],
                "email_confidence": contact["confidence"],
                "hunter_status":    contact["status"],
                "found_at":         _now_utc(),
            })

    # ── ProPublica: email-finder per officer ─────────────────────────────
    print("\n[top5] === ProPublica top 5 ===", file=sys.stderr)
    for rank, company in enumerate(get_top_n(spreadsheet, "ProPublica", n=5), 1):
        name  = company["company_name"]
        score = company["_score"]
        officers = get_officers(spreadsheet, name, max_officers=3)

        if not officers:
            print(f"[top5]   #{rank} '{name}': no officers in Officers tab — skipping",
                  file=sys.stderr)
            all_contacts.append(_blank_row("ProPublica", rank, name, score, "no_officers"))
            continue

        print(f"[top5]   #{rank} '{name}'  —  {len(officers)} officer(s)", file=sys.stderr)
        for officer in officers:
            full_name = officer["name"].strip()
            title     = officer["title"]
            parts = full_name.split()
            if len(parts) < 2:
                print(f"[top5]        Skipping '{full_name}' — can't split name", file=sys.stderr)
                continue
            first, last = parts[0], " ".join(parts[1:])
            result = email_finder(first, last, name, api_key)
            print(f"[top5]        {full_name} ({title}): "
                  f"{result['email'] or result['status']}  conf={result['confidence']}",
                  file=sys.stderr)
            all_contacts.append({
                "source":           "ProPublica",
                "rank":             rank,
                "company_name":     name,
                "normalized_score": score,
                "contact_name":     full_name,
                "contact_title":    title,
                "email":            result["email"],
                "email_confidence": result["confidence"],
                "hunter_status":    result["status"],
                "found_at":         _now_utc(),
            })

    # ── Write to sheet ────────────────────────────────────────────────────
    from pipeline.sheets_sync import _get_or_create_tab
    ws = _get_or_create_tab(spreadsheet, OUTPUT_TAB)
    ws.clear()
    data_rows = [[str(c.get(f, "")) for f in OUTPUT_FIELDS] for c in all_contacts]
    ws.update([OUTPUT_FIELDS] + data_rows)

    found = sum(1 for c in all_contacts if c.get("email"))
    print(
        f"\n[top5] Done — {found}/{len(all_contacts)} contacts with email "
        f"written to '{OUTPUT_TAB}'",
        file=sys.stderr,
    )


def _blank_row(source: str, rank: int, name: str, score: int, status: str) -> dict:
    return {
        "source": source, "rank": rank, "company_name": name,
        "normalized_score": score, "contact_name": "", "contact_title": "",
        "email": "", "email_confidence": "", "hunter_status": status,
        "found_at": _now_utc(),
    }
