"""Alternative Data Ingestion Fleet (`src/ingestion/alt_data`)."""

from src.ingestion.alt_data.agmarknet_mandi import (
    AgmarknetMandiScraper,
    normalize_agri_commodity,
)
from src.ingestion.alt_data.base_scraper import ALT_DATA_LAKE, BaseAltDataScraper
from src.ingestion.alt_data.gem_tenders import GemTendersScraper, parse_bid_value_inr
from src.ingestion.alt_data.grid_india import (
    GridIndiaScraper,
    normalize_grid_region,
    parse_power_gw,
)
from src.ingestion.alt_data.npci_upi import (
    NpciUpiScraper,
    compute_avg_ticket_size_inr,
)
from src.ingestion.alt_data.rbi_statistics import RbiPaymentSystemsScraper
from src.ingestion.alt_data.quick_commerce import (
    STAPLE_BASKET,
    QuickCommerceScraper,
    classify_staple_category,
)
from src.ingestion.alt_data.vahan_dashboard import (
    VahanDashboardScraper,
    classify_fuel_type,
    normalize_maker,
)

__all__ = [
    "ALT_DATA_LAKE",
    "STAPLE_BASKET",
    "AgmarknetMandiScraper",
    "BaseAltDataScraper",
    "GemTendersScraper",
    "GridIndiaScraper",
    "NpciUpiScraper",
    "QuickCommerceScraper",
    "RbiPaymentSystemsScraper",
    "VahanDashboardScraper",
    "classify_fuel_type",
    "classify_staple_category",
    "compute_avg_ticket_size_inr",
    "normalize_agri_commodity",
    "normalize_grid_region",
    "normalize_maker",
    "parse_bid_value_inr",
    "parse_power_gw",
]
