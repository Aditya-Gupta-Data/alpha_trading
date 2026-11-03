"""Nightly alt-data fleet (cron #39): runs all 6 alt-data scrapers into data/lake/alt_data/."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Type

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.ingestion.alt_data import (  # noqa: E402
    AgmarknetMandiScraper,
    BaseAltDataScraper,
    GemTendersScraper,
    GridIndiaScraper,
    NpciUpiScraper,
    QuickCommerceScraper,
    VahanDashboardScraper,
)

FLEET_SCRAPERS: Sequence[Type[BaseAltDataScraper]] = (
    VahanDashboardScraper,
    GemTendersScraper,
    QuickCommerceScraper,
    AgmarknetMandiScraper,
    GridIndiaScraper,
    NpciUpiScraper,
)


def run_fleet(lake_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Instantiate and execute all 6 AltData scrapers cleanly."""
    summaries: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {"lake_dir": lake_dir} if lake_dir is not None else {}
    for scraper_cls in FLEET_SCRAPERS:
        scraper = scraper_cls(**kwargs)
        summary = scraper.run()
        summaries.append(summary)
    return summaries


def main() -> int:
    for summary in run_fleet():
        print(json.dumps(summary, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
