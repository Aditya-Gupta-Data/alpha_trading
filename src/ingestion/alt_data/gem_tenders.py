"""
src/ingestion/alt_data/gem_tenders.py — GeM Public Bid & Tender Results Scraper
================================================================================

Targets the Government e-Marketplace (GeM) public bid and contract award portals:
  * `https://bidplus.gem.gov.in/all-bids` (BidPlus public bid results / RA awards)
  * `https://gem.gov.in/view_contracts` (GeM public contract award registry)

Extracts:
  * `winning_bidder`: Awarded L1 seller / company name (plus mapped NSE ticker
    when matched to a listed entity).
  * `bid_value`: Normalized numeric contract value in INR (parsing raw `₹`, `Rs.`,
    Indian comma separators, `Cr`/`Crore`, and `Lakh`/`Lac` multipliers).
  * `item_category`: Awarded product/service category on GeM.
  * `bid_number` & `date`: GeM Bid/Contract reference and award date for idempotent
    deduplication in `data/lake/alt_data/gem_tenders.csv`.
"""
from __future__ import annotations

import html
import json
import re
from datetime import date as _date
from pathlib import Path
from typing import Any, Callable

from src.ingestion.alt_data.base_scraper import BaseAltDataScraper
from src.ingestion.alt_data.vahan_dashboard import _HTMLTableExtractor

GEM_BIDPLUS_URL = "https://bidplus.gem.gov.in/all-bids"
GEM_CONTRACTS_URL = "https://gem.gov.in/view_contracts"

LISTED_BIDDER_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bBHARAT\s+HEAVY\s+ELECTRICALS|\bBHEL\b", re.I), "BHEL.NS"),
    (re.compile(r"\bBHARAT\s+ELECTRONICS|\bBEL\b", re.I), "BEL.NS"),
    (re.compile(r"\bHINDUSTAN\s+AERONAUTICS|\bHAL\b", re.I), "HAL.NS"),
    (re.compile(r"\bLARSEN\s*(?:&|AND)\s*TOUBRO|\bL&T\b", re.I), "LT.NS"),
    (re.compile(r"\bTATA\s+MOTORS\b", re.I), "TATAMOTORS.NS"),
    (re.compile(r"\bTATA\s+CONSULTANCY|\bTCS\b", re.I), "TCS.NS"),
    (re.compile(r"\bMAHINDRA\s*(?:&|AND)\s*MAHINDRA|\bM&M\b", re.I), "M&M.NS"),
    (re.compile(r"\bMARUTI\s+SUZUKI\b", re.I), "MARUTI.NS"),
    (re.compile(r"\bASHOK\s+LEYLAND\b", re.I), "ASHOKLEY.NS"),
    (re.compile(r"\bBEML\b", re.I), "BEML.NS"),
    (re.compile(r"\bMAZAGON\s+DOCK\b", re.I), "MAZDOCK.NS"),
    (re.compile(r"\bRAIL\s+VIKAS\s+NIGAM|\bRVNL\b", re.I), "RVNL.NS"),
    (re.compile(r"\bIRCON\b", re.I), "IRCON.NS"),
    (re.compile(r"\bINFOSYS\b", re.I), "INFY.NS"),
    (re.compile(r"\bWIPRO\b", re.I), "WIPRO.NS"),
    (re.compile(r"\bHCL\s+TECH", re.I), "HCLTECH.NS"),
)

_CARD_BLOCK_RE = re.compile(
    r'<(?:div|article|li|section)[^>]*class=["\'][^"\']*'
    r'(?:bid-card|card-body|block_header|bid_list|contract-card|tender-item)[^"\']*["\'][^>]*>'
    r"(.*?)</(?:div|article|li|section)>",
    re.I | re.S,
)

_TAG_STRIP_RE = re.compile(r"<[^>]+>")


def match_listed_ticker(bidder_name: str) -> str:
    """Return the NSE ticker if `bidder_name` matches a known listed corporation, else `''`."""
    cleaned = re.sub(r"\s+", " ", str(bidder_name or "")).strip()
    if not cleaned:
        return ""
    for pattern, ticker in LISTED_BIDDER_PATTERNS:
        if pattern.search(cleaned):
            return ticker
    return ""


def parse_bid_value_inr(raw_value: Any) -> float | None:
    """Parse an Indian currency string or number into numeric INR (`float`).

    Examples:
      * `'₹ 1,25,00,000.00'` -> `12500000.0`
      * `'Rs. 4.5 Crore'` / `'4.5 Cr'` -> `45000000.0`
      * `'85.2 Lakh'` / `'85.2 Lac'` -> `8520000.0`
    """
    if raw_value is None:
        return None
    if isinstance(raw_value, (int, float)):
        val = float(raw_value)
        return round(val, 2) if val >= 0 else None

    s = str(raw_value).strip()
    if not s:
        return None

    multiplier = 1.0
    upper = s.upper()
    if re.search(r"\b(?:CRORES?|CR\.?)\b", upper):
        multiplier = 1e7
    elif re.search(r"\b(?:LAKHS?|LACS?)\b", upper):
        multiplier = 1e5
    elif re.search(r"\b(?:MILLIONS?|MN)\b", upper):
        multiplier = 1e6
    elif re.search(r"\b(?:THOUSANDS?|K)\b", upper):
        multiplier = 1e3

    cleaned = re.sub(r"(?:INR|RS\.?|₹|,|\s)", "", s, flags=re.I)
    m = re.search(r"(\d+(?:\.\d+)?)", cleaned)
    if not m:
        return None
    try:
        numeric = float(m.group(1)) * multiplier
        return round(numeric, 2)
    except ValueError:
        return None


def _clean_text(fragment: str) -> str:
    txt = _TAG_STRIP_RE.sub(" ", html.unescape(str(fragment or "")))
    return re.sub(r"\s+", " ", txt).strip()


class GemTendersScraper(BaseAltDataScraper):
    """Government e-Marketplace (GeM) public bid results scraper."""

    dataset_name = "gem_tenders"
    default_columns = (
        "date",
        "bid_number",
        "winning_bidder",
        "ticker",
        "bid_value",
        "item_category",
    )
    primary_key_columns = ("date", "bid_number", "winning_bidder", "item_category")

    def __init__(
        self,
        lake_dir: Path | str | None = None,
        log_path: Path | str | None = None,
        endpoint_url: str = GEM_BIDPLUS_URL,
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
        self.endpoint_url = endpoint_url

    def fetch_raw(self, page: int = 1, **kwargs: Any) -> str:
        """Fetch public GeM bid results page or JSON payload."""
        url = self.endpoint_url
        if page > 1:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}page={int(page)}"
        raw_bytes = self.request(
            url,
            method="GET",
            headers={"Referer": GEM_CONTRACTS_URL},
        )
        return raw_bytes.decode("utf-8", errors="replace")

    def parse(
        self,
        raw: Any,
        award_date: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Parse GeM JSON or HTML bid results into normalized tender award dicts."""
        default_day = str(award_date or _date.today().isoformat())[:10]
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
                doc = json.loads(stripped)
                return self._parse_json_obj(doc, default_day)
            except json.JSONDecodeError:
                pass

        # 1. Try HTML table extraction first
        extractor = _HTMLTableExtractor()
        extractor.feed(html.unescape(text))
        rows: list[dict[str, Any]] = []
        for table in extractor.tables:
            rows.extend(self._parse_html_table(table, default_day))
        if rows:
            return rows

        # 2. Fall back to GeM bid-result card blocks / regex extraction
        return self._parse_html_cards(text, default_day)

    def _normalize_record(
        self,
        winning_bidder: Any,
        bid_value: Any,
        item_category: Any,
        bid_number: Any = None,
        day: str | None = None,
    ) -> dict[str, Any] | None:
        bidder = re.sub(r"\s+", " ", str(winning_bidder or "")).strip()
        category = re.sub(r"\s+", " ", str(item_category or "")).strip()
        val_inr = parse_bid_value_inr(bid_value)
        if not bidder or not category or val_inr is None:
            return None
        row_day = str(day or _date.today().isoformat())[:10]
        bid_no = re.sub(r"\s+", " ", str(bid_number or "")).strip()
        if not bid_no:
            bid_no = f"GEM-{row_day}-{abs(hash((bidder, category, val_inr))) % 1000000:06d}"
        return {
            "date": row_day,
            "bid_number": bid_no,
            "winning_bidder": bidder,
            "ticker": match_listed_ticker(bidder),
            "bid_value": f"{val_inr:.2f}",
            "item_category": category,
        }

    def _parse_json_obj(self, doc: Any, default_day: str) -> list[dict[str, Any]]:
        if isinstance(doc, dict):
            items = (
                doc.get("bids")
                or doc.get("tenders")
                or doc.get("contracts")
                or doc.get("response", {}).get("docs")
                or doc.get("data")
                or []
            )
            default_day = str(doc.get("date") or default_day)[:10]
        elif isinstance(doc, list):
            items = doc
        else:
            return []

        out: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            bidder = (
                item.get("winning_bidder")
                or item.get("seller_name")
                or item.get("awarded_to")
                or item.get("l1_bidder")
                or item.get("company_name")
            )
            value = (
                item.get("bid_value")
                or item.get("contract_value")
                or item.get("total_value")
                or item.get("award_value")
                or item.get("amount")
            )
            category = (
                item.get("item_category")
                or item.get("category")
                or item.get("item_description")
                or item.get("product_category")
            )
            bid_no = (
                item.get("bid_number")
                or item.get("bid_no")
                or item.get("contract_no")
                or item.get("ra_no")
            )
            row_day = str(
                item.get("award_date") or item.get("date") or default_day
            )[:10]
            rec = self._normalize_record(bidder, value, category, bid_no, row_day)
            if rec:
                out.append(rec)
        return out

    def _parse_html_table(
        self, table: list[list[str]], default_day: str
    ) -> list[dict[str, Any]]:
        if len(table) < 2:
            return []
        headers = [h.upper() for h in table[0]]

        def _find_col(keywords: tuple[str, ...]) -> int | None:
            for idx, h in enumerate(headers):
                if any(k in h for k in keywords):
                    return idx
            return None

        bidder_col = _find_col(("WINNING BIDDER", "SELLER", "AWARDED TO", "BIDDER", "L1"))
        value_col = _find_col(("BID VALUE", "CONTRACT VALUE", "TOTAL VALUE", "AMOUNT", "VALUE", "PRICE"))
        cat_col = _find_col(("ITEM CATEGORY", "CATEGORY", "ITEM", "PRODUCT", "DESCRIPTION"))
        bid_col = _find_col(("BID NO", "BID NUMBER", "CONTRACT NO", "TENDER NO", "RA NO"))
        date_col = _find_col(("DATE", "AWARD DATE"))

        if bidder_col is None or value_col is None or cat_col is None:
            return []

        out: list[dict[str, Any]] = []
        for r in table[1:]:
            if max(bidder_col, value_col, cat_col) >= len(r):
                continue
            bid_no = r[bid_col] if (bid_col is not None and bid_col < len(r)) else None
            row_day = (
                r[date_col][:10]
                if (date_col is not None and date_col < len(r) and r[date_col])
                else default_day
            )
            rec = self._normalize_record(
                r[bidder_col], r[value_col], r[cat_col], bid_no, row_day
            )
            if rec:
                out.append(rec)
        return out

    def _parse_html_cards(self, text: str, default_day: str) -> list[dict[str, Any]]:
        blocks = _CARD_BLOCK_RE.findall(text) or [text]
        out: list[dict[str, Any]] = []
        stop_lookahead = (
            r"(?=\s*(?:\||;|\n|"
            r"Winning\s+Bidder|Awarded\s+To|L1\s+Seller|Seller\s+Name|"
            r"Bid\s+Value|Contract\s+Value|Total\s+Value|Award\s+Value|"
            r"Item\s+Category|Category|Product\s+Category|"
            r"Bid\s+(?:No|Number)|Contract\s+(?:No|Number)|$))"
        )
        for block in blocks:
            clean = _clean_text(block)
            if not clean:
                continue
            bidder_m = re.search(
                r"(?:Winning\s+Bidder|Awarded\s+To|L1\s+Seller|Seller\s+Name)\s*[:\-]\s*([^|;\n]+?)"
                + stop_lookahead,
                clean,
                re.I,
            )
            val_m = re.search(
                r"(?:Bid\s+Value|Contract\s+Value|Total\s+Value|Award\s+Value)\s*[:\-]\s*"
                r"((?:INR|Rs\.?|₹)?\s*[\d,]+(?:\.\d+)?(?:\s*(?:Cr|Crore|Lakh|Lac)s?)?)",
                clean,
                re.I,
            )
            cat_m = re.search(
                r"(?:Item\s+Category|Category|Product\s+Category)\s*[:\-]\s*([^|;\n]+?)"
                + stop_lookahead,
                clean,
                re.I,
            )
            bid_m = re.search(
                r"(?:Bid\s+(?:No|Number)|Contract\s+(?:No|Number))\s*[:\-]?\s*([A-Z0-9/\-]+)",
                clean,
                re.I,
            )
            if bidder_m and val_m and cat_m:
                rec = self._normalize_record(
                    bidder_m.group(1),
                    val_m.group(1),
                    cat_m.group(1),
                    bid_m.group(1) if bid_m else None,
                    default_day,
                )
                if rec:
                    out.append(rec)
        return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="GeM public tender & bid results scraper")
    ap.add_argument("--date", default=None, help="Award date YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="Parse only; do not write CSV")
    args = ap.parse_args()
    scraper = GemTendersScraper()
    print(json.dumps(scraper.run(dry_run=args.dry_run, award_date=args.date), indent=2))
