"""Shared data model for all ESG Funding Intelligence scrapers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class FundingRecord:
    company_name: str = ""
    source: str = ""              # "bcorp" | "grants_gov" | "sephora_accelerate" | etc.
    source_track: str = ""        # "Grants & Funding" | "ESG & Corporate Partners"
    disclosure_status: bool = False
    score_or_rating: str = ""
    sector: str = ""
    year_of_disclosure: Optional[int] = None
    report_url: str = ""
    funding_type: str = ""        # "corporate_sponsor" | "grant" | "government"
    beauty_alignment: bool = False
    sustainability_keywords: List[str] = field(default_factory=list)
    is_open: Optional[bool] = None  # None = unknown / not applicable
    scraped_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    notes: str = ""
