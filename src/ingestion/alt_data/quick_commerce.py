"""
src/ingestion/alt_data/quick_commerce.py — Quick-Commerce Real-Time CPI Scraper
===============================================================================

Targets undocumented JSON search/catalog endpoints for Zepto and Blinkit to track
a fixed staple basket (`Wheat`, `Cooking Oil`, `Milk`, `FMCG`) and compute a daily
Laspeyres-style Custom CPI (`base = 100.0`).

Endpoints:
  * Blinkit: `https://blinkit.com/v1/layout/search`
  * Zepto:   `https://api.zeptonow.com/api/v3/search`

Output (`data/lake/alt_data/quick_commerce_cpi.csv`):
  Columns: `date, platform, category, avg_price_inr, baseline_price_inr,
            category_index, custom_cpi, inflation_pct, sample_size`
"""
from __future__ import annotations

import json
import re
from datetime import date as _date
from pathlib import Path
from typing import Any, Callable

from src.ingestion.alt_data.base_scraper import BaseAltDataScraper

BLINKIT_SEARCH_URL = "https://blinkit.com/v1/layout/search"
ZEPTO_SEARCH_URL = "https://api.zeptonow.com/api/v3/search"

# Fixed CPI staple basket: category -> {weight, baseline_inr, query, keywords}
STAPLE_BASKET: dict[str, dict[str, Any]] = {
    "Wheat": {
        "weight": 0.30,
        "baseline_inr": 240.0,
        "query": "wheat atta 5kg",
        "keywords": ("wheat", "atta", "chakki", "aashirvaad", "pillsbury", "flour"),
    },
    "Cooking Oil": {
        "weight": 0.25,
        "baseline_inr": 145.0,
        "query": "sunflower cooking oil 1l",
        "keywords": (
            "cooking oil",
            "sunflower oil",
            "mustard oil",
            "soyabean oil",
            "soybean oil",
            "refined oil",
            "fortune",
            "saffola",
            "dhara",
        ),
    },
    "Milk": {
        "weight": 0.25,
        "baseline_inr": 66.0,
        "query": "toned milk 1l",
        "keywords": ("milk", "amul", "mother dairy", "nandini", "toned", "full cream"),
    },
    "FMCG": {
        "weight": 0.20,
        "baseline_inr": 120.0,
        "query": "detergent soap biscuit tea",
        "keywords": (
            "fmcg",
            "detergent",
            "surf excel",
            "ariel",
            "soap",
            "lux",
            "lifebuoy",
            "biscuit",
            "parle",
            "britannia",
            "tea",
            "tata tea",
            "brooke bond",
            "toothpaste",
            "colgate",
        ),
    },
}

BASKET_CATEGORIES: tuple[str, ...] = ("Wheat", "Cooking Oil", "Milk", "FMCG")


def classify_staple_category(name_or_cat: str) -> str | None:
    """Classify a product title or category string into one of the 4 fixed staple categories."""
    text = re.sub(r"\s+", " ", str(name_or_cat or "")).strip().lower()
    if not text:
        return None
    for cat in BASKET_CATEGORIES:
        if text == cat.lower():
            return cat
    for cat, spec in STAPLE_BASKET.items():
        if any(kw in text for kw in spec["keywords"]):
            return cat
    return None


def _extract_price(item: dict[str, Any]) -> float | None:
    """Extract numeric INR selling price from a Blinkit or Zepto item dict."""
    for key in (
        "selling_price",
        "price",
        "price_inr",
        "discounted_price",
        "normal_price",
        "mrp",
        "sellingPrice",
        "discountedSellingPrice",
    ):
        raw = item.get(key)
        if raw is None:
            continue
        if isinstance(raw, dict):
            raw = raw.get("value") or raw.get("amount") or raw.get("price")
        if raw is None:
            continue
        cleaned = re.sub(r"(?:INR|Rs\.?|₹|,|\s)", "", str(raw), flags=re.I)
        m = re.search(r"(\d+(?:\.\d+)?)", cleaned)
        if not m:
            continue
        val = float(m.group(1))
        if val <= 0:
            continue
        # Zepto v3 sometimes quotes prices in paise (e.g. 24500 for Rs 245.00)
        if str(item.get("currency_unit") or item.get("unit") or "").lower() == "paise" or (
            key in ("sellingPrice", "discountedSellingPrice") and val >= 5000.0
        ):
            val = val / 100.0
        return round(val, 2)
    return None


class QuickCommerceScraper(BaseAltDataScraper):
    """Quick-Commerce (Blinkit / Zepto) staple basket scraper & daily custom CPI calculator."""

    dataset_name = "quick_commerce_cpi"
    default_columns = (
        "date",
        "platform",
        "category",
        "avg_price_inr",
        "baseline_price_inr",
        "category_index",
        "custom_cpi",
        "inflation_pct",
        "sample_size",
    )
    primary_key_columns = ("date", "platform", "category")

    def __init__(
        self,
        lake_dir: Path | str | None = None,
        log_path: Path | str | None = None,
        platforms: tuple[str, ...] = ("blinkit", "zepto"),
        lat: str = "19.0760",
        lon: str = "72.8777",
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
        self.platforms = tuple(p.lower() for p in platforms)
        self.lat = str(lat)
        self.lon = str(lon)

    def fetch_raw(self, **kwargs: Any) -> dict[str, list[Any]]:
        """Fetch undocumented search JSON payloads for each staple category across configured platforms."""
        payloads: dict[str, list[Any]] = {}
        for platform in self.platforms:
            platform_docs: list[Any] = []
            for category, spec in STAPLE_BASKET.items():
                q = spec["query"].replace(" ", "+")
                if platform == "blinkit":
                    url = f"{BLINKIT_SEARCH_URL}?q={q}"
                    headers = {
                        "Accept": "application/json",
                        "lat": self.lat,
                        "lon": self.lon,
                        "app_client": "consumer_web",
                    }
                else:
                    url = f"{ZEPTO_SEARCH_URL}?query={q}"
                    headers = {
                        "Accept": "application/json",
                        "x-latitude": self.lat,
                        "x-longitude": self.lon,
                        "app-platform": "web",
                    }
                raw_bytes = self.request(url, method="GET", headers=headers)
                text = raw_bytes.decode("utf-8", errors="replace").strip()
                if text:
                    try:
                        doc = json.loads(text)
                    except json.JSONDecodeError:
                        doc = {"raw_text": text, "category": category}
                    platform_docs.append({"category_hint": category, "payload": doc})
            payloads[platform] = platform_docs
        return payloads

    def _extract_items_from_doc(
        self, doc: Any, default_platform: str, category_hint: str | None = None
    ) -> list[dict[str, Any]]:
        if isinstance(doc, (bytes, bytearray)):
            doc = doc.decode("utf-8", errors="replace")
        if isinstance(doc, str):
            try:
                doc = json.loads(doc)
            except json.JSONDecodeError:
                return []

        if isinstance(doc, list):
            out: list[dict[str, Any]] = []
            for elem in doc:
                out.extend(
                    self._extract_items_from_doc(elem, default_platform, category_hint)
                )
            return out

        if not isinstance(doc, dict):
            return []

        platform = str(doc.get("platform") or default_platform or "blinkit").lower()
        hint = str(doc.get("category_hint") or doc.get("category") or category_hint or "")

        if "payload" in doc:
            return self._extract_items_from_doc(doc["payload"], platform, hint)

        # Collect candidate product lists from Blinkit / Zepto undocumented JSON schemas
        candidates: list[Any] = []
        for list_key in ("products", "items", "storeProducts", "snippets", "objects", "data", "results"):
            val = doc.get(list_key)
            if isinstance(val, list):
                candidates.extend(val)
            elif isinstance(val, dict):
                for sub_key in ("products", "items", "snippets", "objects", "results"):
                    if isinstance(val.get(sub_key), list):
                        candidates.extend(val[sub_key])

        # Also support direct single-product dicts
        if not candidates and any(
            k in doc for k in ("name", "title", "product_name", "selling_price", "price", "sellingPrice")
        ):
            candidates = [doc]

        extracted: list[dict[str, Any]] = []
        for prod in candidates:
            if not isinstance(prod, dict):
                continue
            inner = prod.get("data") if isinstance(prod.get("data"), dict) else prod
            if isinstance(inner.get("product"), dict):
                merged = dict(inner["product"])
                for k, v in inner.items():
                    if k != "product":
                        merged.setdefault(k, v)
                inner = merged

            title = (
                inner.get("name")
                or inner.get("title")
                or inner.get("product_name")
                or inner.get("display_name")
                or inner.get("category")
                or hint
            )
            cat = (
                classify_staple_category(inner.get("category") or "")
                or classify_staple_category(title)
                or classify_staple_category(hint)
            )
            price = _extract_price(inner)
            if cat and price is not None:
                extracted.append(
                    {
                        "platform": str(inner.get("platform") or platform).lower(),
                        "category": cat,
                        "price_inr": price,
                    }
                )
        return extracted

    def parse(
        self,
        raw: Any,
        report_date: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Parse quick-commerce JSON payload(s) and compute category indices + weighted Custom CPI."""
        day = str(report_date or _date.today().isoformat())[:10]
        if isinstance(raw, dict) and str(raw.get("date") or ""):
            day = str(raw["date"])[:10]

        items: list[dict[str, Any]] = []
        if isinstance(raw, dict) and any(k in ("blinkit", "zepto") for k in raw):
            for plat, docs in raw.items():
                if plat in ("date", "as_of"):
                    continue
                items.extend(self._extract_items_from_doc(docs, str(plat).lower()))
        else:
            default_plat = self.platforms[0] if self.platforms else "blinkit"
            items.extend(self._extract_items_from_doc(raw, default_plat))

        if not items:
            return []

        # Group observed prices by (platform, category)
        grouped: dict[tuple[str, str], list[float]] = {}
        platforms_seen: set[str] = set()
        for it in items:
            plat = it["platform"]
            cat = it["category"]
            platforms_seen.add(plat)
            grouped.setdefault((plat, cat), []).append(float(it["price_inr"]))

        rows: list[dict[str, Any]] = []
        for plat in sorted(platforms_seen):
            cat_stats: dict[str, tuple[float, float, float, int]] = {}
            weighted_sum = 0.0
            weight_total = 0.0

            for cat in BASKET_CATEGORIES:
                prices = grouped.get((plat, cat))
                if not prices:
                    continue
                avg_px = sum(prices) / len(prices)
                base_px = float(STAPLE_BASKET[cat]["baseline_inr"])
                weight = float(STAPLE_BASKET[cat]["weight"])
                cat_idx = (avg_px / base_px) * 100.0
                cat_stats[cat] = (avg_px, base_px, cat_idx, len(prices))
                weighted_sum += weight * cat_idx
                weight_total += weight

            if not cat_stats or weight_total <= 0:
                continue

            custom_cpi = round(weighted_sum / weight_total, 2)
            inflation_pct = round(custom_cpi - 100.0, 2)

            for cat in BASKET_CATEGORIES:
                if cat not in cat_stats:
                    continue
                avg_px, base_px, cat_idx, n = cat_stats[cat]
                rows.append(
                    {
                        "date": day,
                        "platform": plat,
                        "category": cat,
                        "avg_price_inr": f"{avg_px:.2f}",
                        "baseline_price_inr": f"{base_px:.2f}",
                        "category_index": f"{cat_idx:.2f}",
                        "custom_cpi": f"{custom_cpi:.2f}",
                        "inflation_pct": f"{inflation_pct:.2f}",
                        "sample_size": n,
                    }
                )
        return rows


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Quick-Commerce Real-Time CPI Scraper")
    ap.add_argument("--date", default=None, help="Report date YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="Parse only; do not write CSV")
    args = ap.parse_args()
    scraper = QuickCommerceScraper()
    print(json.dumps(scraper.run(dry_run=args.dry_run, report_date=args.date), indent=2))
