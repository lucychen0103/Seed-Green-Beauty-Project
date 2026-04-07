"""ESG Funding Intelligence Platform — main entry point.

Runs all scrapers sequentially, merges output, and syncs to Google Sheets.
"""

import asyncio
import sys


async def run_all(source: str = "all"):
    """Run scrapers for the specified source(s)."""
    # TODO: Import and run individual scrapers
    print(f"Running scrapers for: {source}")


if __name__ == "__main__":
    source = sys.argv[1] if len(sys.argv) > 1 else "all"
    asyncio.run(run_all(source))
