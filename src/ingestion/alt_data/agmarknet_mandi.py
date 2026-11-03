"""
src/ingestion/alt_data/agmarknet_mandi.py — Agmarknet 2.0 state-wise wholesale price scraper
===========================================================================================

Source (verified live 2026-11-03): agmarknet.gov.in is a JS app over `https://api.agmarknet.gov.in/v1/`.
`POST dashboard-data/` with `dashboard = "statewise_price_arrival_sp"` (the public homepage
dashboard, no captcha) returns each state's weighted-average wholesale price for the chosen
commodities over `from_date..to_date`. The market-wise report is captcha-gated; this module never
calls it (playbook ground rule 5).

One call per day (from_date = to_date), so every row is a single day's state average. Commodities:
Wheat (1), Sugar (44), Tomatoes (65), Onions (23).

Output (`data/lake/alt_data/agmarknet_mandi.csv`, key date + commodity + state + mandi):
  `date, commodity, state, mandi, min_price_quintal, max_price_quintal, modal_price_quintal,
   modal_price_kg, price_1y_ago_quintal, unit`
  mandi = `STATE_WEIGHTED_AVG`; min/max are blank (the dashboard gives only the weighted average).
  A state with no reported price that day is skipped, never filled.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any, Callable

from src.ingestion.alt_data.base_scraper import IST, BaseAltDataScraper

AGMARKNET_API_URL = "https://api.agmarknet.gov.in/v1/dashboard-data/"
AGMARKNET_ORIGIN = "https://agmarknet.gov.in"
STATEWISE_DASHBOARD = "statewise_price_arrival_sp"
STATE_AVG_MANDI = "STATE_WEIGHTED_AVG"
DEFAULT_DAYS_PER_RUN = 3
PAGE_LIMIT = 100
MAX_PAGES = 10

AGMARKNET_COMMODITIES: dict[str, dict[str, Any]] = {
    "Wheat": {"code": 1, "aliases": ("wheat", "gehu", "kanak")},
    "Sugar": {"code": 44, "aliases": ("sugar", "cheeni")},
    "Tomatoes": {"code": 65, "aliases": ("tomato", "tomatoes", "tamatar")},
    "Onions": {"code": 23, "aliases": ("onion", "onions", "pyaz", "kanda")},
}

CORE_AGRI_COMMODITIES: tuple[str, ...] = ("Wheat", "Sugar", "Tomatoes", "Onions")
_EXCLUDED = re.compile(r"\b(green|snap|atta|cane|seed)\b", re.I)


def normalize_agri_commodity(raw_name: str) -> str | None:
    """Normalize an Agmarknet commodity string onto `Wheat`, `Sugar`, `Tomatoes`, or `Onions`."""
    cleaned = re.sub(r"\s+", " ", str(raw_name or "")).strip().lower()
    if not cleaned or _EXCLUDED.search(cleaned):
        return None
    for canonical, spec in AGMARKNET_COMMODITIES.items():
        if cleaned == canonical.lower() or any(
            re.search(rf"\b{re.escape(alias)}\b", cleaned) for alias in spec["aliases"]
        ):
            return canonical
    return None


def _price(raw: Any) -> float | None:
    try:
        val = float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return round(val, 2) if val > 0 else None


class AgmarknetMandiScraper(BaseAltDataScraper):
    """Agmarknet state-wise weighted-average wholesale prices for Wheat, Sugar, Tomatoes, Onions."""

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
        "price_1y_ago_quintal",
        "unit",
    )
    primary_key_columns = ("date", "commodity", "state", "mandi")

    def __init__(
        self,
        lake_dir: Path | str | None = None,
        log_path: Path | str | None = None,
        commodities: tuple[str, ...] = CORE_AGRI_COMMODITIES,
        days_per_run: int = DEFAULT_DAYS_PER_RUN,
        fetch_fn: Callable[..., bytes | str] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        today_fn: Callable[[], _dt.date] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(lake_dir=lake_dir, log_path=log_path, fetch_fn=fetch_fn, sleep_fn=sleep_fn, **kwargs)
        self.commodities = tuple(c for c in commodities if c in AGMARKNET_COMMODITIES)
        self.days_per_run = max(1, int(days_per_run))
        self._today = today_fn or (lambda: _dt.datetime.now(IST).date())

    def _days(self, report_date: str | None) -> list[str]:
        if report_date:
            return [str(report_date)[:10]]
        today = self._today()
        return [(today - _dt.timedelta(days=i)).isoformat() for i in range(self.days_per_run)]

    def fetch_day(self, day: str) -> list[dict[str, Any]]:
        """All state records for one day across the configured commodities (follows pagination)."""
        records: list[dict[str, Any]] = []
        codes = [AGMARKNET_COMMODITIES[c]["code"] for c in self.commodities]
        for page in range(1, MAX_PAGES + 1):
            body = {"dashboard": STATEWISE_DASHBOARD, "from_date": day, "to_date": day,
                    "commodity": codes, "format": "json", "page": page, "limit": PAGE_LIMIT}
            raw = self.request(
                AGMARKNET_API_URL, method="POST", data=json.dumps(body),
                headers={"Content-Type": "application/json", "Origin": AGMARKNET_ORIGIN,
                         "Referer": AGMARKNET_ORIGIN + "/"},
            )
            doc = json.loads(raw.decode("utf-8", errors="replace"))
            if doc.get("status") is False:
                if re.search(r"no data", str(doc.get("message", "")), re.I):
                    break
                raise ValueError(f"agmarknet refused {day}: {doc.get('message')}")
            data = doc.get("data") or {}
            records.extend(data.get("records") or [] if isinstance(data, dict) else [])
            if not (doc.get("pagination") or {}).get("next_page"):
                break
        return records

    def fetch_raw(self, report_date: str | None = None, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"date": day, "records": self.fetch_day(day)} for day in self._days(report_date)]

    def parse(self, raw: Any, **kwargs: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for doc in raw or []:
            day = doc.get("date")
            for rec in doc.get("records") or []:
                comm = normalize_agri_commodity(rec.get("cmdt_name", ""))
                state = str(rec.get("state_name") or "").strip()
                px = _price(rec.get("as_on"))
                if not (day and comm and state and px):
                    continue
                unit = str(rec.get("unit_name_price") or "").strip()
                per_kg = round(px / 100.0, 2) if "quintal" in unit.lower() else None
                out.append({
                    "date": day,
                    "commodity": comm,
                    "state": state,
                    "mandi": STATE_AVG_MANDI,
                    "min_price_quintal": "",
                    "max_price_quintal": "",
                    "modal_price_quintal": f"{px:.2f}",
                    "modal_price_kg": "" if per_kg is None else f"{per_kg:.2f}",
                    "price_1y_ago_quintal": "" if _price(rec.get("one_yr_ago")) is None else f"{_price(rec.get('one_yr_ago')):.2f}",
                    "unit": unit,
                })
        return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Agmarknet 2.0 state-wise wholesale price scraper")
    ap.add_argument("--date", default=None, help="Price date YYYY-MM-DD (default: the last 3 days)")
    ap.add_argument("--dry-run", action="store_true", help="Parse only; do not write CSV")
    args = ap.parse_args()
    scraper = AgmarknetMandiScraper()
    print(json.dumps(scraper.run(dry_run=args.dry_run, report_date=args.date), indent=2))
