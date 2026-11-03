"""Alternative Data Ingestion Fleet (`src/ingestion/alt_data`)."""

from src.ingestion.alt_data.base_scraper import ALT_DATA_LAKE, BaseAltDataScraper
from src.ingestion.alt_data.gem_tenders import GemTendersScraper, parse_bid_value_inr
from src.ingestion.alt_data.vahan_dashboard import (
    VahanDashboardScraper,
    classify_fuel_type,
    normalize_maker,
)

__all__ = [
    "ALT_DATA_LAKE",
    "BaseAltDataScraper",
    "GemTendersScraper",
    "VahanDashboardScraper",
    "classify_fuel_type",
    "normalize_maker",
    "parse_bid_value_inr",
]
