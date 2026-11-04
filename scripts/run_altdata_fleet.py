"""Nightly alt-data fleet (#39, Mini PC only): runs 5 alt-data scrapers into data/lake/alt_data/."""
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
    RbiPaymentSystemsScraper,
    VahanDashboardScraper,
)

FLEET_SCRAPERS: Sequence[Type[BaseAltDataScraper]] = (
    VahanDashboardScraper,
    GemTendersScraper,
    AgmarknetMandiScraper,
    GridIndiaScraper,
    RbiPaymentSystemsScraper,
)


def run_fleet(lake_dir: Optional[Path] = None, refresh_index=None) -> List[Dict[str, Any]]:
    """Run every scraper in FLEET_SCRAPERS (Quick-Commerce: ToS; NPCI: Akamai bot wall, replaced by RBI — playbook rule 5).
    First refreshes GeM's NSE name index when it is over a week old (fail-open)."""
    from src.ingestion.alt_data.gem_tenders import refresh_name_index
    summaries: List[Dict[str, Any]] = [{"dataset": "nse_name_index", **(refresh_index or refresh_name_index)()}]
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
