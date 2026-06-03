"""Sort Master tab rows by score_or_rating (descending) within each source group.

Source order is preserved: propublica → cdp → bcorp.

Usage:
    python _sort_master_tab.py
"""

import sys
sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv(dotenv_path=".env")

from pipeline.sheets_sync import get_spreadsheet

SOURCE_ORDER = ["propublica", "cdp", "bcorp"]
SCORE_COL = 3  # score_or_rating


def _score_key(row):
    try:
        return float(row[SCORE_COL].strip())
    except ValueError:
        return 0.0


def sort_master():
    spreadsheet = get_spreadsheet()
    ws = spreadsheet.worksheet("Master")
    rows = ws.get_all_values()
    header = rows[0]
    data = rows[1:]

    groups = {
        src: sorted([r for r in data if r[1] == src], key=_score_key, reverse=True)
        for src in SOURCE_ORDER
    }

    sorted_data = []
    for src in SOURCE_ORDER:
        sorted_data.extend(groups[src])
        top3 = [r[SCORE_COL] for r in groups[src][:3]]
        print(f"{src}: {len(groups[src])} rows, top 3 scores: {top3}")

    ws.clear()
    ws.update([header] + sorted_data, value_input_option="RAW")
    print("\nMaster tab sorted successfully.")


if __name__ == "__main__":
    sort_master()
