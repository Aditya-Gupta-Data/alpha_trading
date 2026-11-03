"""
src/ingestion/alt_data/agmarknet_mandi.py — MoA Agmarknet Wholesale Mandi Scraper
=================================================================================

Targets the Ministry of Agriculture & Farmers Welfare (MoA) Agmarknet portal
(`https://agmarknet.gov.in/SearchCmmMkt.aspx`) to extract daily wholesale mandi
prices (Min, Max, and Modal price in INR/Quintal and INR/kg) for the 4 core
agricultural inflation & margin commodities:
  * `Wheat`
  * `Sugar`
  * `Tomatoes`
  * `Onions`

Output (`data/lake/alt_data/agmarknet_mandi.csv`):
  Columns: `date, commodity, state, mandi, min_price_quintal,
            max_price_quintal, modal_price_quintal, modal_price_kg`
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import re
from pathlib import Path
from typing import Any, Callable

from src.ingestion.alt_data.base_scraper import BaseAltDataScraper
from src.ingestion.alt_data.vahan_dashboard import _HTMLTableExtractor

AGMARKNET_BASE_URL = "https://agmarknet.gov.in/"
AGMARKNET_SEARCH_URL = "https://agmarknet.gov.in/SearchCmmMkt.aspx"

# MoA Agmarknet commodity codes & canonical names
AGMARKNET_COMMODITIES: dict[str, dict[str, Any]] = {
    "Wheat": {"code": "1", "aliases": ("wheat", "gehu", "kanak")},
    "Sugar": {"code": "48", "aliases": ("sugar", "cheeni", "gur")},
    "Tomatoes": {"code": "78", "aliases": ("tomato", "tomatoes", "tamatar")},
    "Onions": {"code": "23", "aliases": ("onion", "onions", "pyaz", "kanda")},
}

CORE_AGRI_COMMODITIES: tuple[str, ...] = ("Wheat", "Sugar", "Tomatoes", "Onions")


def normalize_agri_commodity(raw_name: str) -> str | None:
    """Normalize an Agmarknet commodity string onto `Wheat`, `Sugar`, `Tomatoes`, or `Onions`."""
    cleaned = re.sub(r"\s+", " ", str(raw_name or "")).strip().lower()
    if not cleaned:
        return None
    for canonical, spec in AGMARKNET_COMMODITIES.items():
        if cleaned == canonical.lower() or any(
            re.search(rf"\b{re.escape(alias)}\b", cleaned) for alias in spec["aliases"]
        ):
            return canonical
    return None


def _parse_price(raw_val: Any) -> float | None:
    if raw_val is None:
        return None
    s = re.sub(r"(?:INR|Rs\.?|₹|,|\s)", "", str(raw_val), flags=re.I)
    if not s or s in {"-", "NR", "NA", "N/A"}:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return None
    val = float(m.group(1))
    return round(val, 2) if val > 0 else None


def _normalize_date(raw_date: Any, default_day: str) -> str:
    s = str(raw_date or "").strip()
    if not s:
        return default_day
    # ISO YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    # Agmarknet DD-Mon-YYYY or DD/MM/YYYY or DD-MM-YYYY
    for fmt in ("%d-%b-%Y", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return _dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return default_day


class AgmarknetMandiScraper(BaseAltDataScraper):
    """MoA Agmarknet wholesale mandi price scraper for Wheat, Sugar, Tomatoes, and Onions."""

    dataset_name = "agmarknet_mandi"
    default_columns = (
        "date",
        "commodity",
        "state",
        "mandi",
        "min_price_quintal",
        "max_price_quintal",
        "modal_price_quintal",
        "modal_price_kg",
    )
    primary_key_columns = ("date", "commodity", "state", "mandi")

    def __init__(
        self,
        lake_dir: Path | str | None = None,
        log_path: Path | str | None = None,
        commodities: tuple[str, ...] = CORE_AGRI_COMMODITIES,
        fetch_fn: Callable[..., bytes | str] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            lake_dir=lake_dir,
            log_path=log_path,
            fetch_fn=fetch_fn,
            sleep_fn=sleep_fn,
            **kwargs,
        )
        self.commodities = tuple(commodities)

    def fetch_raw(self, report_date: str | None = None, **kwargs: Any) -> list[str]:
        """Fetch Agmarknet mandi price pages across the 4 target commodities."""
        day_iso = str(report_date or _dt.date.today().isoformat())[:10]
        try:
            dt_str = _dt.date.fromisoformat(day_iso).strftime("%d-%b-%Y")
        except ValueError:
            dt_str = day_iso

        pages: list[str] = []
        for comm in self.commodities:
            spec = AGMARKNET_COMMODITIES.get(comm, {"code": "1"})
            url = (
                f"{AGMARKNET_SEARCH_URL}?Tx_Commodity={spec['code']}"
                f"&Tx_State=0&Tx_District=0&Tx_Market=0"
                f"&DateFrom={dt_str}&DateTo={dt_str}"
                f"&Fr_Date={dt_str}&To_Date={dt_str}&Tx_Trend=0"
                f"&Tx_CommodityHead={comm}"
            )
            raw_bytes = self.request(
                url,
                method="GET",
                headers={"Referer": AGMARKNET_BASE_URL},
            )
            pages.append(raw_bytes.decode("utf-8", errors="replace"))
        return pages

    def parse(
        self,
        raw: Any,
        report_date: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Parse Agmarknet HTML GridView tables or JSON payloads into normalized mandi price rows."""
        default_day = str(report_date or _dt.date.today().isoformat())[:10]
        if isinstance(raw, list) and raw and all(isinstance(x, str) for x in raw):
            out: list[dict[str, Any]] = []
            for page in raw:
                out.extend(self._parse_single_payload(page, default_day))
            return out
        return self._parse_single_payload(raw, default_day)

    def _parse_single_payload(self, raw: Any, default_day: str) -> list[dict[str, Any]]:
        if isinstance(raw, (bytes, bytearray)):
            text = raw.decode("utf-8", errors="replace")
        elif isinstance(raw, (dict, list)):
            return self._parse_json_obj(raw, default_day)
        else:
            text = str(raw or "")

        stripped = text.strip()
        if not stripped:
            return []

        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return self._parse_json_obj(json.loads(stripped), default_day)
            except json.JSONDecodeError:
                pass

        extractor = _HTMLTableExtractor()
        extractor.feed(html.unescape(text))
        rows: list[dict[str, Any]] = []
        for table in extractor.tables:
            rows.extend(self._parse_html_table(table, default_day))
        return rows

    def _build_row(
        self,
        commodity_raw: Any,
        state_raw: Any,
        mandi_raw: Any,
        min_px_raw: Any,
        max_px_raw: Any,
        modal_px_raw: Any,
        date_raw: Any,
        default_day: str,
    ) -> dict[str, Any] | None:
        commodity = normalize_agri_commodity(str(commodity_raw or ""))
        if not commodity:
            return None
        modal_q = _parse_price(modal_px_raw)
        min_q = _parse_price(min_px_raw)
        max_q = _parse_price(max_px_raw)
        if modal_q is None:
            if min_q is not None and max_q is not None:
                modal_q = round((min_q + max_q) / 2.0, 2)
            else:
                return None
        if min_q is None:
            min_q = modal_q
        if max_q is None:
            max_q = modal_q

        state = re.sub(r"\s+", " ", str(state_raw or "All India")).strip() or "All India"
        mandi = re.sub(r"\s+", " ", str(mandi_raw or "National")).strip() or "National"
        day = _normalize_date(date_raw, default_day)
        modal_kg = round(modal_q / 100.0, 2)

        return {
            "date": day,
            "commodity": commodity,
            "state": state,
            "mandi": mandi,
            "min_price_quintal": f"{min_q:.2f}",
            "max_price_quintal": f"{max_q:.2f}",
            "modal_price_quintal": f"{modal_q:.2f}",
            "modal_price_kg": f"{modal_kg:.2f}",
        }

    def _parse_json_obj(self, doc: Any, default_day: str) -> list[dict[str, Any]]:
        if isinstance(doc, dict):
            items = doc.get("records") or doc.get("data") or doc.get("prices") or []
            default_day = _normalize_date(doc.get("date"), default_day)
        elif isinstance(doc, list):
            items = doc
        else:
            return []

        out: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            row = self._build_row(
                commodity_raw=item.get("commodity") or item.get("Commodity"),
                state_raw=item.get("state") or item.get("State"),
                mandi_raw=item.get("mandi") or item.get("market") or item.get("Market") or item.get("district"),
                min_px_raw=item.get("min_price") or item.get("min_price_quintal") or item.get("Min_x0020_Price"),
                max_px_raw=item.get("max_price") or item.get("max_price_quintal") or item.get("Max_x0020_Price"),
                modal_px_raw=item.get("modal_price") or item.get("modal_price_quintal") or item.get("Modal_x0020_Price"),
                date_raw=item.get("arrival_date") or item.get("price_date") or item.get("date"),
                default_day=default_day,
            )
            if row:
                out.append(row)
        return out

    def _parse_html_table(
        self, table: list[list[str]], default_day: str
    ) -> list[dict[str, Any]]:
        if len(table) < 2:
            return []
        headers = [h.upper() for h in table[0]]

        def _col(keywords: tuple[str, ...]) -> int | None:
            for k in keywords:
                for idx, h in enumerate(headers):
                    if k in h:
                        return idx
            return None

        comm_col = _col(("COMMODITY", "CROP", "ITEM"))
        state_col = _col(("STATE",))
        mandi_col = _col(("MARKET", "MANDI", "CENTER", "CENTRE", "DISTRICT"))
        min_col = _col(("MIN PRICE", "MIN", "MINIMUM"))
        max_col = _col(("MAX PRICE", "MAX", "MAXIMUM"))
        modal_col = _col(("MODAL PRICE", "MODAL", "AVG PRICE", "PRICE"))
        date_col = _col(("PRICE DATE", "ARRIVAL DATE", "DATE"))

        if comm_col is None or modal_col is None:
            return []

        out: list[dict[str, Any]] = []
        for r in table[1:]:
            if max(comm_col, modal_col) >= len(r):
                continue
            row = self._build_row(
                commodity_raw=r[comm_col],
                state_raw=r[state_col] if (state_col is not None and state_col < len(r)) else "All India",
                mandi_raw=r[mandi_col] if (mandi_col is not None and mandi_col < len(r)) else "National",
                min_px_raw=r[min_col] if (min_col is not None and min_col < len(r)) else None,
                max_px_raw=r[max_col] if (max_col is not None and max_col < len(r)) else None,
                modal_px_raw=r[modal_col],
                date_raw=r[date_col] if (date_col is not None and date_col < len(r)) else None,
                default_day=default_day,
            )
            if row:
                out.append(row)
        return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="MoA Agmarknet wholesale mandi scraper")
    ap.add_argument("--date", default=None, help="Report date YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="Parse only; do not write CSV")
    args = ap.parse_args()
    scraper = AgmarknetMandiScraper()
    print(json.dumps(scraper.run(dry_run=args.dry_run, report_date=args.date), indent=2))
