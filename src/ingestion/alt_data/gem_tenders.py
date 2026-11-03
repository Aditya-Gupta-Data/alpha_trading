"""
src/ingestion/alt_data/gem_tenders.py — GeM awarded high-value bids scraper
===========================================================================

Source (verified live 2026-11-03), the public BidPlus pages a browser uses, no login, no captcha:
  1. `GET https://bidplus.gem.gov.in/all-bids` — sets the session cookie and carries the page's
     `csrf_bd_gem_nk` token.
  2. `POST https://bidplus.gem.gov.in/all-bids-data` (form: `payload` JSON + token) with the filter
     "Bid/RA Awarded" + "High Value", newest bid end date first, 10 bids per page.
  3. `GET https://bidplus.gem.gov.in/bidding/bid/getBidResultView/<b_id>` — the public result page;
     its "List of Sellers Qualified Financially" table gives each seller's total price and rank.

One row per awarded bid: the L1 seller and its total price. `date` is the bid END date (GeM does
not publish the award date on the listing). Bids already in the CSV are not re-fetched; result
pages are fetched with a pause between them.

Output (`data/lake/alt_data/gem_tenders.csv`, key date + bid_number + winning_bidder + item_category):
  `date, bid_number, winning_bidder, ticker, bid_value, item_category, ministry, department, bid_id, sellers`
  `ticker` is set only when the L1 seller matches a known listed company (`LISTED_BIDDER_PATTERNS`).
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any, Callable

from src.ingestion.alt_data.base_scraper import BaseAltDataScraper
from src.ingestion.alt_data.vahan_dashboard import _HTMLTableExtractor

GEM_BIDPLUS_URL = "https://bidplus.gem.gov.in/all-bids"
GEM_BIDS_DATA_URL = "https://bidplus.gem.gov.in/all-bids-data"
GEM_RESULT_URL = "https://bidplus.gem.gov.in/bidding/bid/getBidResultView/{b_id}"
DEFAULT_MAX_PAGES = 5
DEFAULT_RESULT_PAUSE_S = 1.0

# Full names match anywhere; bare abbreviations only as the whole seller name (optionally "Ltd").
_ABBR = r"^\s*(?:M/S\.?\s*)?{}\s*(?:LTD\.?|LIMITED)?\s*$"
LISTED_BIDDER_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bBHARAT\s+HEAVY\s+ELECTRICALS|" + _ABBR.format("BHEL"), re.I), "BHEL.NS"),
    (re.compile(r"\bBHARAT\s+ELECTRONICS\s+(?:LTD|LIMITED)|" + _ABBR.format("BEL"), re.I), "BEL.NS"),
    (re.compile(r"\bHINDUSTAN\s+AERONAUTICS|" + _ABBR.format("HAL"), re.I), "HAL.NS"),
    (re.compile(r"\bLARSEN\s*(?:&|AND)\s*TOUBRO|" + _ABBR.format("L&T"), re.I), "LT.NS"),
    (re.compile(r"\bTATA\s+MOTORS\b", re.I), "TATAMOTORS.NS"),
    (re.compile(r"\bTATA\s+CONSULTANCY|" + _ABBR.format("TCS"), re.I), "TCS.NS"),
    (re.compile(r"\bMAHINDRA\s*(?:&|AND)\s*MAHINDRA\s+(?:LTD|LIMITED)|" + _ABBR.format("M&M"), re.I), "M&M.NS"),
    (re.compile(r"\bMARUTI\s+SUZUKI\b", re.I), "MARUTI.NS"),
    (re.compile(r"\bASHOK\s+LEYLAND\b", re.I), "ASHOKLEY.NS"),
    (re.compile(r"\bBEML\s+(?:LTD|LIMITED)|" + _ABBR.format("BEML"), re.I), "BEML.NS"),
    (re.compile(r"\bMAZAGON\s+DOCK\b", re.I), "MAZDOCK.NS"),
    (re.compile(r"\bRAIL\s+VIKAS\s+NIGAM|" + _ABBR.format("RVNL"), re.I), "RVNL.NS"),
    (re.compile(r"\bIRCON\s+INTERNATIONAL|" + _ABBR.format("IRCON"), re.I), "IRCON.NS"),
    (re.compile(r"\bINFOSYS\s+(?:LTD|LIMITED)|" + _ABBR.format("INFOSYS"), re.I), "INFY.NS"),
    (re.compile(r"\bWIPRO\s+(?:LTD|LIMITED)|" + _ABBR.format("WIPRO"), re.I), "WIPRO.NS"),
    (re.compile(r"\bHCL\s+TECHNOLOGIES\b", re.I), "HCLTECH.NS"),
)

_SELLER_TAG_RE = re.compile(r"\s*\((?:MSE|MII|SC|ST|WOMEN|STARTUP|,|\s)+\)|\s+Under\s+PMA\b.*$", re.I)


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
    """Parse an Indian currency string or number into INR (`'₹ 1,25,00,000.00'`, `'4.5 Cr'`, `'85.2 Lakh'`)."""
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
    cleaned = re.sub(r"(?:INR|RS\.?|₹|`|,|\s)", "", s, flags=re.I)
    m = re.search(r"(\d+(?:\.\d+)?)", cleaned)
    if not m:
        return None
    return round(float(m.group(1)) * multiplier, 2)


def clean_seller_name(raw: str) -> str:
    """`'SHREE HARI ENTERPRISE(MII) Under PMA'` -> `'SHREE HARI ENTERPRISE'`."""
    name = _SELLER_TAG_RE.sub("", str(raw or ""))
    return re.sub(r"\s+", " ", name).strip(" ,")


def parse_result_sellers(page_html: str) -> list[dict[str, Any]]:
    """Financial-evaluation table -> [{seller, price, rank}] in rank order; [] when absent."""
    extractor = _HTMLTableExtractor()
    extractor.feed(html.unescape(page_html or ""))
    for table in extractor.tables:
        if not table:
            continue
        head = [c.strip().lower() for c in table[0]]
        if "seller name" not in head or "rank" not in head or not any("price" in h for h in head):
            continue
        si, ri = head.index("seller name"), head.index("rank")
        pi = next(i for i, h in enumerate(head) if "price" in h)
        out = []
        for row in table[1:]:
            if max(si, ri, pi) >= len(row):
                continue
            rank = row[ri].strip().upper()
            price = parse_bid_value_inr(row[pi])
            if re.fullmatch(r"L\d+", rank) and price is not None:
                out.append({"seller": clean_seller_name(row[si]), "price": price, "rank": rank})
        out.sort(key=lambda r: int(r["rank"][1:]))
        return out
    return []


def _first(doc: dict[str, Any], key: str) -> Any:
    val = doc.get(key)
    return val[0] if isinstance(val, list) and val else val


class GemTendersScraper(BaseAltDataScraper):
    """GeM awarded high-value bids: L1 seller, value, buyer ministry and category."""

    dataset_name = "gem_tenders"
    default_columns = (
        "date",
        "bid_number",
        "winning_bidder",
        "ticker",
        "bid_value",
        "item_category",
        "ministry",
        "department",
        "bid_id",
        "sellers",
    )
    primary_key_columns = ("date", "bid_number", "winning_bidder", "item_category")

    def __init__(
        self,
        lake_dir: Path | str | None = None,
        log_path: Path | str | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        result_pause_s: float = DEFAULT_RESULT_PAUSE_S,
        fetch_fn: Callable[..., bytes | str] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(lake_dir=lake_dir, log_path=log_path, fetch_fn=fetch_fn, sleep_fn=sleep_fn, **kwargs)
        self.max_pages = max(1, int(max_pages))
        self.result_pause_s = max(0.0, float(result_pause_s))

    def _csrf_token(self) -> str:
        page = self.request(GEM_BIDPLUS_URL).decode("utf-8", errors="replace")
        m = re.search(r"csrf_bd_gem_nk['\"]\s*:\s*['\"]([0-9a-f]+)", page)
        if not m:
            raise ValueError("all-bids page carried no csrf_bd_gem_nk token")
        return m.group(1)

    def list_awarded(self, token: str, page: int) -> list[dict[str, Any]]:
        payload = {
            "page": page,
            "param": {"searchBid": "", "searchType": "fullText"},
            "filter": {"bidStatusType": "bidrastatus", "byStatus": "bid_awarded", "byType": "all",
                       "highBidValue": "highValue", "byEndDate": {"from": "", "to": ""},
                       "sort": "Bid-End-Date-Latest"},
        }
        raw = self.request(
            GEM_BIDS_DATA_URL, method="POST",
            data={"payload": json.dumps(payload), "csrf_bd_gem_nk": token},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": GEM_BIDPLUS_URL},
        )
        doc = json.loads(raw.decode("utf-8", errors="replace"))
        if doc.get("code") != 200:
            raise ValueError(f"all-bids-data returned code {doc.get('code')}")
        return ((doc.get("response") or {}).get("response") or {}).get("docs") or []

    def fetch_raw(self, **kwargs: Any) -> list[dict[str, Any]]:
        known = {str(r.get("bid_number", "")).strip() for r in self.read_existing_rows()}
        token = self._csrf_token()
        out: list[dict[str, Any]] = []
        for page in range(1, self.max_pages + 1):
            docs = self.list_awarded(token, page)
            if not docs:
                break
            fresh = [d for d in docs if str(_first(d, "b_bid_number") or "") not in known]
            for d in fresh:
                b_id = _first(d, "b_id")
                if b_id is None:
                    continue
                if out:
                    self._sleep_fn(self.result_pause_s)
                page_html = self.request(GEM_RESULT_URL.format(b_id=int(b_id)), headers={"Referer": GEM_BIDPLUS_URL})
                out.append({"bid": d, "result_html": page_html.decode("utf-8", errors="replace")})
            if not fresh:
                break
        return out

    def parse(self, raw: Any, **kwargs: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in raw or []:
            d = item.get("bid") or {}
            sellers = parse_result_sellers(item.get("result_html", ""))
            bid_no = str(_first(d, "b_bid_number") or "").strip()
            end = str(_first(d, "final_end_date_sort") or "")[:10]
            if not sellers:
                self.log_warning("NO-L1", f"{bid_no}: result page had no financial ranking")
                continue
            if not (bid_no and re.fullmatch(r"\d{4}-\d{2}-\d{2}", end)):
                continue
            l1 = sellers[0]
            rows.append({
                "date": end,
                "bid_number": bid_no,
                "winning_bidder": l1["seller"],
                "ticker": match_listed_ticker(l1["seller"]),
                "bid_value": f"{l1['price']:.2f}",
                "item_category": str(_first(d, "b_category_name") or _first(d, "bd_category_name") or "").strip()[:200],
                "ministry": str(_first(d, "ba_official_details_minName") or "").strip(),
                "department": str(_first(d, "ba_official_details_deptName") or "").strip(),
                "bid_id": str(_first(d, "b_id") or ""),
                "sellers": len(sellers),
            })
        return rows


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="GeM awarded high-value bids scraper")
    ap.add_argument("--pages", type=int, default=DEFAULT_MAX_PAGES, help="Listing pages to scan (10 bids each)")
    ap.add_argument("--dry-run", action="store_true", help="Parse only; do not write CSV")
    args = ap.parse_args()
    scraper = GemTendersScraper(max_pages=args.pages)
    print(json.dumps(scraper.run(dry_run=args.dry_run), indent=2))
