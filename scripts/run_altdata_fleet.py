"""Nightly alt-data fleet (cron #39): runs the Vahan and GeM scrapers into data/lake/alt_data/."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.ingestion.alt_data import GemTendersScraper, VahanDashboardScraper  # noqa: E402


def main() -> int:
    for scraper in (VahanDashboardScraper(), GemTendersScraper()):
        print(json.dumps(scraper.run(), default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
