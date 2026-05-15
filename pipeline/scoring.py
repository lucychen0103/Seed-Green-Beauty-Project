"""Normalized 0-100 sustainability score computation across all data sources.

Scoring rules by source
-----------------------
CDP
  Primary:  Performance Band from the notes field
              A=100, A-=90, B=75, C=55, D=35, F=10
  Fallback: Disclosure Score (numeric 0-100) when no band is present

ProPublica
  Has filed 990s                     +40 pts
  Environment sector (NTEE code C*)  +40 pts
  Sustainability keyword in org name +10 pts per keyword, capped at +20

Future sources (placeholders — return 0 until scrapers are live)
  B Corp  : B Impact Score ÷ 2  (raw scale 0-200 → 0-100)
  UNGC    : Active signatory=70, Advanced COP=100
  GRI     : Has current report=60, Core=75, Comprehensive=100
"""

PERFORMANCE_BAND_MAP = {
    "A": 100,
    "A-": 90,
    "B": 75,
    "C": 55,
    "D": 35,
    "F": 10,
}

# Must match SUSTAINABILITY_NAME_KEYWORDS in scrapers/propublica.py
_SUSTAINABILITY_NAME_KEYWORDS = (
    "green", "sustain", "environ", "eco", "natural", "clean",
    "organic", "climate", "earth", "conserv", "renew",
)

_ENV_SECTORS = {
    "Environment",
    "Pollution Abatement & Control",
    "Natural Resources Conservation",
    "Water/Air/Waste Management",
}


def compute_normalized_score(record: dict) -> int:
    """Return a 0-100 normalized sustainability score for *record*."""
    source = record.get("source", "")
    if source == "cdp":
        return _score_cdp(record)
    if source == "propublica":
        return _score_propublica(record)
    return 0


def _score_cdp(record: dict) -> int:
    notes = record.get("notes", "")
    band = ""
    if "Performance Band:" in notes:
        band = notes.split("Performance Band:")[1].split("|")[0].strip()

    if band in PERFORMANCE_BAND_MAP:
        return PERFORMANCE_BAND_MAP[band]

    # Fall back to numeric Disclosure Score
    raw = record.get("score_or_rating", "")
    try:
        return min(100, max(0, int(float(raw))))
    except (ValueError, TypeError):
        return 0


def _score_propublica(record: dict) -> int:
    score = 0

    # +40: has filed 990s (disclosure_status may be bool or string after sheet round-trip)
    if record.get("disclosure_status") in (True, "True", "true"):
        score += 40

    # +40: Environment sector (NTEE code C*)
    if record.get("sector", "") in _ENV_SECTORS:
        score += 40

    # +10 per sustainability keyword in org name, capped at +20
    name = record.get("company_name", "").lower()
    kw_hits = sum(1 for kw in _SUSTAINABILITY_NAME_KEYWORDS if kw in name)
    score += min(20, kw_hits * 10)

    return min(100, score)
