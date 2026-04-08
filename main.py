"""ESG Funding Intelligence Platform — main entry point.

Runs all scrapers sequentially, merges output, and syncs to Google Sheets.

Usage:
    python main.py              # run all scrapers
    python main.py cdp          # run CDP scraper only
    python main.py propublica   # run ProPublica scraper only
"""

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

def _load_env(env_path: Path) -> None:
    """Load .env file, handling multi-line values like JSON blocks."""
    if not env_path.exists():
        return
    with open(env_path, "r") as f:
        content = f.read()
    # Try standard dotenv first; fall back to manual parse for multi-line values
    import os, re
    pattern = re.compile(r'^([A-Z_]+)=([\s\S]*?)(?=\n[A-Z_]+=|\Z)', re.MULTILINE)
    for match in pattern.finditer(content):
        key, value = match.group(1), match.group(2).strip()
        if key not in os.environ:
            os.environ[key] = value

_load_env(Path(__file__).parent / ".env")

from pipeline import sheets_sync
from scrapers import cdp, propublica


async def run_all(source: str = "all") -> None:
    """Run scrapers for the specified source(s)."""
    summary: dict[str, str] = {}

    if source in ("all", "cdp"):
        try:
            records = await cdp.run()
            if records:
                sheets_sync.sync(records, "CDP")
                summary["CDP"] = f"{len(records)} records synced"
            else:
                summary["CDP"] = "0 records — sync skipped"
        except Exception as exc:
            print(f"[cdp] FAILED: {exc}", file=sys.stderr)
            summary["CDP"] = "FAILED (see stderr)"

    if source in ("all", "propublica"):
        try:
            records = propublica.run()
            if records:
                sheets_sync.sync(records, "ProPublica")
                summary["ProPublica"] = f"{len(records)} records synced"
            else:
                summary["ProPublica"] = "0 records — sync skipped"
        except Exception as exc:
            print(f"[propublica] FAILED: {exc}", file=sys.stderr)
            summary["ProPublica"] = "FAILED (see stderr)"

    print("\n=== Run Summary ===")
    for src, status in summary.items():
        print(f"  {src}: {status}")


if __name__ == "__main__":
    source = sys.argv[1] if len(sys.argv) > 1 else "all"
    valid_sources = {"all", "cdp", "propublica"}
    if source not in valid_sources:
        print(f"Unknown source '{source}'. Valid options: {', '.join(sorted(valid_sources))}", file=sys.stderr)
        sys.exit(1)
    asyncio.run(run_all(source))
