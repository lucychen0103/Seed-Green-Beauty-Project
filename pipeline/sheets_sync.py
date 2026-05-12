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

from pipeline.scoring import compute_normalized_score

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SCHEMA_FIELDS = [
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
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
    return client.open_by_url(url)


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


# ---------------------------------------------------------------------------
# Scoring Guide tab — dedicated sheet with full criteria explanations.
# Layout: 5 columns (A–E).  All formatting applied in one batchUpdate call.
#   A  label / criterion / band        ~240 px
#   B  score / points                  ~80  px
#   C  secondary label (action level)  ~130 px
#   D  explanation                     ~320 px
#   E  padding                         ~20  px
# ---------------------------------------------------------------------------

SCORING_GUIDE_TAB = "Scoring Guide"

# ── colour palette (RGB fractions 0–1) ────────────────────────────────────
_DK_GREEN   = {"red": 0.067, "green": 0.294, "blue": 0.149}   # #114b26
_MD_GREEN   = {"red": 0.176, "green": 0.490, "blue": 0.196}   # #2d7d32
_LT_GREEN   = {"red": 0.824, "green": 0.933, "blue": 0.824}   # #d2edd2
_MINT       = {"red": 0.941, "green": 0.976, "blue": 0.941}   # #f0f9f0
_GRAY       = {"red": 0.925, "green": 0.925, "blue": 0.925}   # #ececec
_WHITE      = {"red": 1.0,   "green": 1.0,   "blue": 1.0  }
_WHITE_TXT  = {"red": 1.0,   "green": 1.0,   "blue": 1.0  }

# ── format presets ────────────────────────────────────────────────────────
_F_TITLE   = {"backgroundColor": _DK_GREEN,  "textFormat": {"foregroundColor": _WHITE_TXT, "bold": True,  "fontSize": 14}, "horizontalAlignment": "CENTER"}
_F_SUBTITLE= {"backgroundColor": _LT_GREEN,  "textFormat": {"italic": True, "fontSize": 10}, "horizontalAlignment": "CENTER"}
_F_SECTION = {"backgroundColor": _MD_GREEN,  "textFormat": {"foregroundColor": _WHITE_TXT, "bold": True,  "fontSize": 11}}
_F_SUBSECT = {"backgroundColor": _LT_GREEN,  "textFormat": {"bold": True,  "fontSize": 10}}
_F_TBL_HDR = {"backgroundColor": _GRAY,      "textFormat": {"bold": True,  "fontSize": 10}}
_F_BODY    = {"backgroundColor": _WHITE,      "textFormat": {"fontSize": 10}, "wrapStrategy": "WRAP"}
_F_ALT     = {"backgroundColor": _MINT,       "textFormat": {"fontSize": 10}, "wrapStrategy": "WRAP"}
_F_CAPTION = {"backgroundColor": _MINT,       "textFormat": {"italic": True, "fontSize": 9},  "wrapStrategy": "WRAP"}


def _col_idx(col: str) -> int:
    """'A' -> 0, 'B' -> 1, 'E' -> 4, etc."""
    result = 0
    for ch in col.upper():
        result = result * 26 + (ord(ch) - ord("A") + 1)
    return result - 1


def _grid(a1: str, sheet_id: int) -> dict:
    """Convert 'A1:E3' or 'A1' to a Sheets API GridRange dict."""
    import re
    m = re.fullmatch(r"([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?", a1)
    c1 = _col_idx(m.group(1));  r1 = int(m.group(2)) - 1
    c2 = (_col_idx(m.group(3)) + 1) if m.group(3) else c1 + 1
    r2 = int(m.group(4))        if m.group(4) else r1 + 1
    return {"sheetId": sheet_id, "startRowIndex": r1, "endRowIndex": r2,
            "startColumnIndex": c1, "endColumnIndex": c2}


def _fmt_request(a1: str, fmt: dict, sheet_id: int) -> dict:
    """Build a repeatCell batchUpdate request from a format preset dict."""
    ue: dict = {}
    if "backgroundColor"     in fmt: ue["backgroundColor"]     = fmt["backgroundColor"]
    if "horizontalAlignment" in fmt: ue["horizontalAlignment"] = fmt["horizontalAlignment"]
    if "wrapStrategy"        in fmt: ue["wrapStrategy"]        = fmt["wrapStrategy"]
    if "textFormat" in fmt:
        src = fmt["textFormat"]
        tf: dict = {}
        for k in ("bold", "italic", "fontSize", "foregroundColor"):
            if k in src:
                tf[k] = src[k]
        ue["textFormat"] = tf
    return {"repeatCell": {"range": _grid(a1, sheet_id), "cell": {"userEnteredFormat": ue}, "fields": "userEnteredFormat"}}


def _build_scoring_guide() -> tuple:
    """Return (rows, api_requests_builder) where api_requests_builder(sheet_id) -> list[dict]."""
    rows: list[list] = []
    _merges: list[str] = []   # "A1:E1" style, resolved after rows are built
    _fmts:   list[tuple] = [] # (range_str, fmt_preset)

    def _rn() -> int:
        return len(rows)  # 1-based after next append

    def blank():
        rows.append(["", "", "", "", ""])

    def full(text, fmt=None):
        rows.append([text, "", "", "", ""])
        n = _rn()
        _merges.append(f"A{n}:E{n}")
        if fmt:
            _fmts.append((f"A{n}:E{n}", fmt))

    def tbl_hdr(a, b, c, d):
        rows.append([a, b, c, d, ""])
        n = _rn()
        _fmts.append((f"A{n}:E{n}", _F_TBL_HDR))

    def tbl_row(a, b, c, d, alt=False):
        rows.append([a, b, c, d, ""])
        n = _rn()
        _fmts.append((f"A{n}:E{n}", _F_ALT if alt else _F_BODY))

    def para(text, fmt=None):
        full(text, fmt or _F_BODY)

    def caption(text):
        full(text, _F_CAPTION)

    # ════════════════════════════════════════════════════════════════════
    # TITLE
    # ════════════════════════════════════════════════════════════════════
    full("SEED Green Beauty – Sustainability Scoring Guide", _F_TITLE)
    full("Column: normalized_score  ·  Scale: 0–100  ·  Higher = stronger sustainability credentials", _F_SUBTITLE)
    blank()

    # ════════════════════════════════════════════════════════════════════
    # OVERVIEW
    # ════════════════════════════════════════════════════════════════════
    full("OVERVIEW", _F_SECTION)
    blank()
    full("Why a normalized score?", _F_SUBSECT)
    para("Each data source uses a completely different rating system. CDP uses letter-grade Performance Bands (A–F). ProPublica's API returns a search-relevance number that has nothing to do with sustainability. Future sources like B Corp use a 0–200 scale, and UNGC/GRI each have their own categorical levels.")
    para("A normalized 0–100 score makes records directly comparable across all sources — so you can sort, filter, and rank companies regardless of where the data originally came from.")
    blank()
    full("How to Read the Score", _F_SUBSECT)
    blank()
    tbl_hdr("Score Range", "Grade", "", "What It Means")
    tbl_row("90 – 100", "Excellent", "", "Highest tier — top-rated climate action or fully verified sustainability credentials",          alt=False)
    tbl_row("75 – 89",  "Strong",    "", "Strong sustainability credentials with demonstrated, measurable action",                        alt=True)
    tbl_row("55 – 74",  "Moderate",  "", "Engaged with sustainability, but not yet at leadership or certification level",                 alt=False)
    tbl_row("35 – 54",  "Basic",     "", "Some engagement — basic disclosure or filing history; limited evidence of action",              alt=True)
    tbl_row("0 – 34",   "Minimal",   "", "Minimal or no sustainability signals detected in available data",                               alt=False)
    blank()

    # ════════════════════════════════════════════════════════════════════
    # CDP
    # ════════════════════════════════════════════════════════════════════
    blank()
    full("CDP  (Carbon Disclosure Project)", _F_SECTION)
    blank()
    full("What is CDP?", _F_SUBSECT)
    para("CDP is a global non-profit that runs the world's largest corporate climate disclosure system. Companies voluntarily report their carbon emissions, climate risks, and mitigation strategies each year. The datasets used here (2011–2013) come from CDP's open data portal and cover Global 500 companies.")
    caption("Full questionnaire responses require a paid CDP subscription. This pipeline captures the two publicly available metrics only: Performance Band and Disclosure Score.")
    blank()
    full("The Two CDP Metrics", _F_SUBSECT)
    blank()
    tbl_hdr("Metric", "", "", "What It Measures")
    tbl_row("Performance Band  (A, A-, B, C, D, F)", "", "", "The quality of a company's actual climate action — what they did, not just what they reported.", alt=False)
    tbl_row("Disclosure Score  (numeric, 0–100)",     "", "", "How thoroughly the company completed the questionnaire — completeness of reporting, not quality of action.", alt=True)
    blank()
    full("Why Performance Band takes priority over Disclosure Score", _F_SUBSECT)
    para("A company could achieve a high Disclosure Score simply by filling out every field of the questionnaire, without meaningfully reducing emissions or setting credible targets.")
    para("Band A, by contrast, requires demonstrated leadership: verified emissions reductions, science-based targets, and third-party assurance. The Band is a much stronger signal of genuine sustainability commitment. The Disclosure Score is used only as a fallback when no band is present in the data.")
    blank()
    full("Score Mapping", _F_SUBSECT)
    blank()
    tbl_hdr("Band", "Score", "Action Level", "What Companies at This Level Demonstrate")
    tbl_row("A",       "100", "Leadership",      "Verified emissions reductions, science-based targets, third-party assurance",                   alt=False)
    tbl_row("A-",      "90",  "Strong",          "Strong climate action with minor gaps compared to full A criteria",                             alt=True)
    tbl_row("B",       "75",  "Taking Action",   "Meaningful climate program in place, but not yet at leadership level",                          alt=False)
    tbl_row("C",       "55",  "Awareness",       "Climate program exists but limited demonstrated action or results",                             alt=True)
    tbl_row("D",       "35",  "Disclosure Only", "Reported to CDP but with minimal evidence of actual climate action",                            alt=False)
    tbl_row("F",       "10",  "Insufficient",    "Failed to disclose adequately or did not respond to CDP",                                       alt=True)
    tbl_row("No band", "—",   "Fallback",        "Disclosure Score used directly as normalized_score (0–100, no transformation)",                 alt=False)
    blank()

    # ════════════════════════════════════════════════════════════════════
    # PROPUBLICA
    # ════════════════════════════════════════════════════════════════════
    blank()
    full("PROPUBLICA  (Nonprofit Explorer)", _F_SECTION)
    blank()
    full("What is ProPublica Nonprofit Explorer?", _F_SUBSECT)
    para("ProPublica's Nonprofit Explorer indexes IRS Form 990 filings for U.S. nonprofits. It is used here to identify sustainability-aligned foundations linked to beauty brands — for example, environmental grantmakers, brand-affiliated foundations focused on clean beauty or conservation, and nonprofits in the NTEE Environment sector (code C*).")
    blank()
    full("Why ProPublica's own 'score' field is NOT used", _F_SUBSECT)
    para("ProPublica's raw score is a search-relevance rank — it measures how closely the org name matched the query used to find it (e.g. 'cosmetics', 'skincare'). A high raw score means the name was a strong keyword match, not that the organization is sustainable.")
    caption("This search-relevance number is preserved in the score_or_rating column for reference, but plays no role in normalized_score.")
    blank()
    full("How the Score is Built  (additive model, max 100)", _F_SUBSECT)
    blank()
    tbl_hdr("Criterion", "Points", "", "Why This Matters")
    tbl_row("Has filed 990s  (disclosure_status = True)",    "+40",            "", "Filing 990s is the baseline for nonprofit accountability and transparency. An org with no filings cannot be independently verified.",                                                                   alt=False)
    tbl_row("Environment sector  (NTEE code C*)",            "+40",            "", "NTEE code C designates orgs whose primary mission is environmental — conservation, pollution control, climate, water/air. The strongest sustainability signal available in ProPublica data.",             alt=True)
    tbl_row("Sustainability keyword in org name  (max ×2)",  "+10 each → +20", "", "Keywords like 'green', 'eco', 'climate', 'organic' in the org name provide supplemental evidence. Capped at 2 keywords to avoid over-weighting the name alone.",                                       alt=False)
    blank()
    full("Keywords Checked", _F_SUBSECT)
    para("green  ·  sustain  ·  environ  ·  eco  ·  natural  ·  clean  ·  organic  ·  climate  ·  earth  ·  conserv  ·  renew")
    blank()
    full("Score Interpretation", _F_SUBSECT)
    blank()
    tbl_hdr("Score", "", "", "Combination of Criteria That Produces It")
    tbl_row("100",  "", "", "Has 990 filings  +  Environment sector  +  2+ sustainability keywords in name", alt=False)
    tbl_row("80",   "", "", "Has 990 filings  +  Environment sector  (no sustainability keywords in name)",  alt=True)
    tbl_row("60",   "", "", "Has 990 filings  +  2+ sustainability keywords  (not classified NTEE=C)",       alt=False)
    tbl_row("40",   "", "", "Has 990 filings only  (or NTEE=C but no filings — unusual edge case)",          alt=True)
    tbl_row("0–20", "", "", "No filings, no NTEE=C  (low-confidence record)",                                alt=False)
    blank()

    # ════════════════════════════════════════════════════════════════════
    # FUTURE SOURCES
    # ════════════════════════════════════════════════════════════════════
    blank()
    full("FUTURE SOURCES  (not yet live)", _F_SECTION)
    blank()
    full("B Corp", _F_SUBSECT)
    para("B Corp certification is awarded by the non-profit B Lab to companies meeting rigorous standards of verified social and environmental performance, accountability, and transparency. The B Impact Score runs 0–200; a minimum of 80 is required for certification.")
    caption("Mapping: B Impact Score ÷ 2 → 0–100.  Example: a score of 120 becomes normalized_score = 60.")
    blank()
    full("UNGC  (UN Global Compact)", _F_SUBSECT)
    para("The UN Global Compact is the world's largest corporate sustainability initiative. Signatories commit to 10 principles covering human rights, labour standards, environmental responsibility, and anti-corruption. Compliance is self-reported annually via a Communication on Progress (COP).")
    tbl_hdr("Status", "Score", "", "What It Represents")
    tbl_row("Active signatory with current COP", "70", "", "Company is enrolled and meeting reporting obligations",        alt=False)
    tbl_row("Advanced COP",                      "100","", "Highest reporting tier — additional disclosures and targets", alt=True)
    blank()
    full("GRI  (Global Reporting Initiative)", _F_SUBSECT)
    para("GRI is the most widely used international sustainability reporting standard. Scoring is based on report completeness — how thoroughly the company disclosed against GRI's topic-specific standards.")
    tbl_hdr("Report Level",    "Score", "", "What It Requires")
    tbl_row("Any current GRI report", "60", "", "Company publishes a GRI-referenced sustainability report",                                         alt=False)
    tbl_row("GRI Core",               "75", "", "Disclosures on all material topics with at least one indicator each",                             alt=True)
    tbl_row("GRI Comprehensive",      "100","", "Full disclosures on every material topic — the most rigorous level, used as the 100-point ceiling", alt=False)
    blank()

    # ════════════════════════════════════════════════════════════════════
    # NOTES
    # ════════════════════════════════════════════════════════════════════
    blank()
    full("NOTES", _F_SECTION)
    blank()
    para("All scores are computed at scrape time and stored in the normalized_score column on each data tab.")
    para("Scores are recomputed on every pipeline run — they update automatically if source data or criteria change.")
    para("The score_or_rating column preserves the original value from each source unchanged (e.g. the raw CDP Disclosure Score or ProPublica search score).")
    blank()

    def build_requests(sheet_id: int) -> list[dict]:
        reqs: list[dict] = []
        # Unmerge the full sheet range first so re-runs don't conflict
        reqs.append({"unmergeCells": {"range": _grid(f"A1:E{len(rows)}", sheet_id)}})
        # Merge requests
        for rng in _merges:
            reqs.append({"mergeCells": {"range": _grid(rng, sheet_id), "mergeType": "MERGE_ALL"}})
        # Format requests
        for rng, fmt in _fmts:
            reqs.append(_fmt_request(rng, fmt, sheet_id))
        # Column widths: A=240, B=80, C=130, D=320, E=20
        for col_i, px in enumerate([240, 80, 130, 320, 20]):
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": col_i, "endIndex": col_i + 1},
                "properties": {"pixelSize": px}, "fields": "pixelSize",
            }})
        return reqs

    return rows, build_requests


def _write_scoring_guide_tab(spreadsheet: gspread.Spreadsheet) -> None:
    """Create (or overwrite) the Scoring Guide tab with formatted multi-column layout."""
    try:
        ws = spreadsheet.worksheet(SCORING_GUIDE_TAB)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=SCORING_GUIDE_TAB, rows=160, cols=5)

    rows, build_requests = _build_scoring_guide()
    ws.update("A1", rows)
    spreadsheet.batch_update({"requests": build_requests(ws.id)})
    print(f"[sheets] '{SCORING_GUIDE_TAB}' tab: scoring guide written ({len(rows)} rows, formatted)", file=sys.stderr)


def append_to_tab(spreadsheet: gspread.Spreadsheet, tab_name: str, records: list[dict]) -> None:
    """Append only new records to a source tab. Never clears the tab (cumulative history)."""
    ws = _get_or_create_tab(spreadsheet, tab_name)
    existing = ws.get_all_values()

    if not existing:
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
        print(f"[sheets] '{tab_name}' tab: appended {len(new_rows)} new records ({len(records) - len(new_rows)} duplicates skipped)", file=sys.stderr)
    else:
        print(f"[sheets] '{tab_name}': no new records (all {len(records)} already present)", file=sys.stderr)


def sync(records: list[dict], tab_name: str, merge: bool = False) -> None:
    """Main entry point. Writes source tab (overwrite or merge), appends to Master, updates Officers tab.

    merge=True: never clears the tab — appends only new records (cumulative history).
    merge=False (default): clears and rewrites the tab each run.
    """
    if not records:
        print(f"[sheets] '{tab_name}': 0 records — skipping sync", file=sys.stderr)
        return

    # Enrich each record with a normalized score before writing
    for rec in records:
        rec["normalized_score"] = compute_normalized_score(rec)

    client = get_client()
    spreadsheet = _open_spreadsheet(client)

    if merge:
        append_to_tab(spreadsheet, tab_name, records)
    else:
        overwrite_tab(spreadsheet, tab_name, records)
    append_to_master(spreadsheet, records)
    _write_scoring_guide_tab(spreadsheet)

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
