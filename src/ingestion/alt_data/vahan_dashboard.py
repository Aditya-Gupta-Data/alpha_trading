"""
src/ingestion/alt_data/vahan_dashboard.py — MoRTH Vahan Auto Registrations Scraper
==================================================================================

Targets the public Ministry of Road Transport & Highways (MoRTH) Vahan 4 Dashboard
(`https://vahan.parivahan.gov.in/vahan4dashboard/vahan/view/reportview.xhtml`) to
extract daily vehicle registrations grouped by Maker (Tata, M&M, Maruti, and peers)
and Fuel Type (`EV` vs `ICE`).

Architecture:
  * Inherits session persistence, rotating headers, exponential backoff, fail-open
    error handling, and atomic idempotent CSV persistence from `BaseAltDataScraper`.
  * Handles both PrimeFaces JSF `javax.faces.ViewState` HTML/XML partial-response
    tables (`<table>...</table>` with Maker x Fuel columns or Maker rows) and JSON
    summary payloads.
  * Normalizes fragmented RTO legal entity names (e.g., "TATA MOTORS PASSENGER
    VEHICLES LTD" + "TATA PASSENGER ELECTRIC MOBILITY LTD" -> Maker `Tata`,
    `TATAMOTORS.NS`) and classifies raw fuel columns (`ELECTRIC(BOV)`, `PURE EV` ->
    `EV`; `PETROL`, `DIESEL`, `CNG`, `LPG`, `HYBRID` -> `ICE`).
  * Writes idempotently to `data/lake/alt_data/vahan_registrations.csv`.
"""
from __future__ import annotations

import html
import json
import re
from datetime import date as _date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from src.ingestion.alt_data.base_scraper import BaseAltDataScraper

VAHAN_BASE_URL = "https://vahan.parivahan.gov.in/vahan4dashboard/"
VAHAN_REPORT_URL = (
    "https://vahan.parivahan.gov.in/vahan4dashboard/vahan/view/reportview.xhtml"
)

# Canonical Maker normalization: pattern -> (canonical_maker, nse_ticker)
MAKER_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\bTATA\b", re.I), "Tata", "TATAMOTORS.NS"),
    (re.compile(r"\b(?:MAHINDRA|M\s*&\s*M)\b", re.I), "M&M", "M&M.NS"),
    (re.compile(r"\bMARUTI\b", re.I), "Maruti", "MARUTI.NS"),
    (re.compile(r"\bBAJAJ\b", re.I), "Bajaj", "BAJAJ-AUTO.NS"),
    (re.compile(r"\bHERO\s+MOTO", re.I), "Hero", "HEROMOTOCO.NS"),
    (re.compile(r"\b(?:EICHER|ROYAL\s+ENFIELD)\b", re.I), "Eicher", "EICHERMOT.NS"),
    (re.compile(r"\bTVS\b", re.I), "TVS", "TVSMOTOR.NS"),
    (re.compile(r"\bHYUNDAI\b", re.I), "Hyundai", "HYUNDAI.NS"),
    (re.compile(r"\bOLA\s+ELECTRIC\b", re.I), "Ola Electric", "OLAELEC.NS"),
    (re.compile(r"\bASHOK\s+LEYLAND\b", re.I), "Ashok Leyland", "ASHOKLEY.NS"),
)

CORE_MAKERS: frozenset[str] = frozenset({"Tata", "M&M", "Maruti"})

EV_FUEL_KEYWORDS: tuple[str, ...] = (
    "ELECTRIC(BOV)",
    "PURE EV",
    "BATTERY OPERATED",
    "ELECTRIC",
    "BOV",
    "FUEL CELL",
    "EV",
)

ICE_FUEL_KEYWORDS: tuple[str, ...] = (
    "PETROL",
    "DIESEL",
    "CNG",
    "LPG",
    "HYBRID",
    "ETHANOL",
    "LNG",
    "BIO",
    "SOLAR",
    "ICE",
)

_VIEWSTATE_RE = re.compile(
    r'name=["\']javax\.faces\.ViewState["\'][^>]*value=["\']([^"\']+)["\']',
    re.I,
)


def normalize_maker(raw_maker: str) -> tuple[str, str] | None:
    """Map a raw Vahan maker string to `(canonical_maker, nse_ticker)`, or `None` if unrecognized."""
    cleaned = re.sub(r"\s+", " ", str(raw_maker or "")).strip()
    if not cleaned:
        return None
    for pattern, canonical, ticker in MAKER_PATTERNS:
        if pattern.search(cleaned):
            return canonical, ticker
    return None


def classify_fuel_type(raw_fuel: str) -> str | None:
    """Classify a Vahan fuel column or label into `'EV'` or `'ICE'`.
    Returns `None` for totals/serial-number/maker columns."""
    label = re.sub(r"\s+", " ", str(raw_fuel or "")).strip().upper()
    if not label or label in {"S.NO", "S NO", "SR.NO", "SL.NO", "MAKER", "TOTAL", "GRAND TOTAL"}:
        return None
    if label == "EV":
        return "EV"
    if label == "ICE":
        return "ICE"
    # Hybrid petrol/diesel vehicles carry internal combustion engines -> ICE
    if "HYBRID" in label and "PURE EV" not in label:
        return "ICE"
    for kw in EV_FUEL_KEYWORDS:
        if kw in label:
            return "EV"
    for kw in ICE_FUEL_KEYWORDS:
        if kw in label:
            return "ICE"
    return None


def _parse_int(val: Any) -> int:
    s = re.sub(r"[^\d\-]", "", str(val or ""))
    if not s or s == "-":
        return 0
    try:
        return max(0, int(s))
    except ValueError:
        return 0


class _HTMLTableExtractor(HTMLParser):
    """Lightweight stdlib HTML table parser that extracts all `<table>` matrices."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._in_table = False
        self._in_row = False
        self._in_cell = False
        self._cur_table: list[list[str]] = []
        self._cur_row: list[str] = []
        self._cur_cell: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t == "table":
            self._in_table = True
            self._cur_table = []
        elif self._in_table and t == "tr":
            self._in_row = True
            self._cur_row = []
        elif self._in_row and t in ("th", "td"):
            self._in_cell = True
            self._cur_cell = []

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("th", "td") and self._in_cell:
            self._in_cell = False
            text = re.sub(r"\s+", " ", "".join(self._cur_cell)).strip()
            self._cur_row.append(text)
        elif t == "tr" and self._in_row:
            self._in_row = False
            if any(self._cur_row):
                self._cur_table.append(self._cur_row)
        elif t == "table" and self._in_table:
            self._in_table = False
            if self._cur_table:
                self.tables.append(self._cur_table)

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cur_cell.append(data)


def extract_viewstate(raw_html: str) -> str | None:
    """Extract JSF `javax.faces.ViewState` token from Vahan's landing XHTML."""
    m = _VIEWSTATE_RE.search(raw_html or "")
    return m.group(1) if m else None


class VahanDashboardScraper(BaseAltDataScraper):
    """MoRTH Vahan 4 Dashboard scraper for daily Maker x Fuel Type (EV vs ICE) registrations."""

    dataset_name = "vahan_registrations"
    default_columns = ("date", "maker", "ticker", "fuel_type", "registrations")
    primary_key_columns = ("date", "maker", "fuel_type")

    def __init__(
        self,
        lake_dir: Path | str | None = None,
        log_path: Path | str | None = None,
        only_core_makers: bool = False,
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
        self.only_core_makers = bool(only_core_makers)

    def fetch_raw(self, report_date: str | None = None, **kwargs: Any) -> str:
        """Fetch the Vahan report view (performing JSF ViewState handshake when present)."""
        landing_bytes = self.request(
            VAHAN_REPORT_URL,
            method="GET",
            headers={"Referer": VAHAN_BASE_URL},
        )
        landing_text = landing_bytes.decode("utf-8", errors="replace")
        viewstate = extract_viewstate(landing_text)
        if not viewstate:
            return landing_text

        form_payload = {
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": "j_idt31",
            "javax.faces.partial.execute": "@all",
            "javax.faces.partial.render": "combTablePnl",
            "j_idt31": "j_idt31",
            "xaxisVar": "Maker",
            "yaxisVar": "Fuel",
            "javax.faces.ViewState": viewstate,
        }
        if report_date:
            form_payload["selectedDate"] = str(report_date)[:10]

        post_bytes = self.request(
            VAHAN_REPORT_URL,
            method="POST",
            data=form_payload,
            headers={
                "Referer": VAHAN_REPORT_URL,
                "Origin": "https://vahan.parivahan.gov.in",
                "Faces-Request": "partial/ajax",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        return post_bytes.decode("utf-8", errors="replace")

    def parse(
        self,
        raw: Any,
        report_date: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Parse Vahan JSON or HTML/PrimeFaces XML table into grouped `(date, maker, fuel_type)` rows."""
        day = str(report_date or _date.today().isoformat())[:10]
        if isinstance(raw, (bytes, bytearray)):
            text = raw.decode("utf-8", errors="replace")
        elif isinstance(raw, (dict, list)):
            return self._aggregate_records(self._parse_json_obj(raw, day))
        else:
            text = str(raw or "")

        stripped = text.strip()
        if not stripped:
            return []

        if stripped.startswith("{") or stripped.startswith("["):
            try:
                doc = json.loads(stripped)
                return self._aggregate_records(self._parse_json_obj(doc, day))
            except json.JSONDecodeError:
                pass

        # Unwrap PrimeFaces CDATA blocks if present
        cdata_blocks = re.findall(r"<!\[CDATA\[(.*?)\]\]>", text, flags=re.S)
        html_body = "\n".join(cdata_blocks) if cdata_blocks else text
        html_body = html.unescape(html_body)

        extractor = _HTMLTableExtractor()
        extractor.feed(html_body)
        records: list[dict[str, Any]] = []
        for table in extractor.tables:
            records.extend(self._parse_table(table, day))
        return self._aggregate_records(records)

    def _parse_json_obj(self, doc: Any, default_day: str) -> list[dict[str, Any]]:
        items: list[Any]
        if isinstance(doc, dict):
            items = (
                doc.get("records")
                or doc.get("data")
                or doc.get("registrations")
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
            maker_info = normalize_maker(item.get("maker") or item.get("maker_name") or "")
            if not maker_info:
                continue
            maker, ticker = maker_info
            if self.only_core_makers and maker not in CORE_MAKERS:
                continue
            row_day = str(item.get("date") or default_day)[:10]

            # Case 1: explicit fuel_type + registrations fields
            if "fuel_type" in item or "fuel" in item:
                ft = classify_fuel_type(item.get("fuel_type") or item.get("fuel") or "")
                if ft:
                    out.append(
                        {
                            "date": row_day,
                            "maker": maker,
                            "ticker": ticker,
                            "fuel_type": ft,
                            "registrations": _parse_int(
                                item.get("registrations") or item.get("count") or item.get("value")
                            ),
                        }
                    )
                continue

            # Case 2: nested fuels dict or direct fuel columns on the item
            fuel_map = item.get("fuels") if isinstance(item.get("fuels"), dict) else item
            for k, v in fuel_map.items():
                if k in {"date", "maker", "maker_name", "ticker", "total"}:
                    continue
                ft = classify_fuel_type(k)
                if ft:
                    out.append(
                        {
                            "date": row_day,
                            "maker": maker,
                            "ticker": ticker,
                            "fuel_type": ft,
                            "registrations": _parse_int(v),
                        }
                    )
        return out

    def _parse_table(self, table: list[list[str]], day: str) -> list[dict[str, Any]]:
        if len(table) < 2:
            return []

        # Locate the header row that names fuel types or Maker/Fuel columns
        header_idx = 0
        for idx, row in enumerate(table[:4]):
            joined = " ".join(row).upper()
            if "MAKER" in joined or any(classify_fuel_type(c) for c in row):
                header_idx = idx
                if any(classify_fuel_type(c) for c in row):
                    break

        headers = table[header_idx]
        data_rows = table[header_idx + 1 :]

        # Check if table is in long format: [Maker, Fuel, Registrations]
        upper_headers = [h.upper() for h in headers]
        if "FUEL" in upper_headers or "FUEL TYPE" in upper_headers:
            maker_col = next(
                (i for i, h in enumerate(upper_headers) if "MAKER" in h), 0
            )
            fuel_col = next(
                (i for i, h in enumerate(upper_headers) if "FUEL" in h), 1
            )
            val_col = next(
                (
                    i
                    for i, h in enumerate(upper_headers)
                    if any(w in h for w in ("REG", "COUNT", "TOTAL", "VOLUME", "UNITS"))
                ),
                min(len(headers) - 1, 2),
            )
            out: list[dict[str, Any]] = []
            for r in data_rows:
                if max(maker_col, fuel_col, val_col) >= len(r):
                    continue
                maker_info = normalize_maker(r[maker_col])
                ft = classify_fuel_type(r[fuel_col])
                if not maker_info or not ft:
                    continue
                maker, ticker = maker_info
                if self.only_core_makers and maker not in CORE_MAKERS:
                    continue
                out.append(
                    {
                        "date": day,
                        "maker": maker,
                        "ticker": ticker,
                        "fuel_type": ft,
                        "registrations": _parse_int(r[val_col]),
                    }
                )
            if out:
                return out

        # Wide crosstab format (MoRTH Vahan default): [S.No, Maker, ELECTRIC(BOV), PETROL, DIESEL, CNG, ...]
        maker_col = next(
            (i for i, h in enumerate(upper_headers) if "MAKER" in h),
            1 if len(headers) > 2 else 0,
        )
        col_fuel_map: dict[int, str] = {}
        for col_idx, col_name in enumerate(headers):
            if col_idx == maker_col:
                continue
            ft = classify_fuel_type(col_name)
            if ft:
                col_fuel_map[col_idx] = ft

        if not col_fuel_map:
            return []

        out = []
        for r in data_rows:
            if maker_col >= len(r):
                continue
            maker_info = normalize_maker(r[maker_col])
            if not maker_info:
                continue
            maker, ticker = maker_info
            if self.only_core_makers and maker not in CORE_MAKERS:
                continue
            for col_idx, ft in col_fuel_map.items():
                if col_idx < len(r):
                    out.append(
                        {
                            "date": day,
                            "maker": maker,
                            "ticker": ticker,
                            "fuel_type": ft,
                            "registrations": _parse_int(r[col_idx]),
                        }
                    )
        return out

    def _aggregate_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Group and sum registrations by `(date, maker, fuel_type)`, always emitting
        both `EV` and `ICE` rows for each observed `(date, maker)` pair."""
        if not records:
            return []
        totals: dict[tuple[str, str, str, str], int] = {}
        seen_makers: dict[tuple[str, str], str] = {}
        for r in records:
            day = str(r["date"])[:10]
            maker = str(r["maker"])
            ticker = str(r["ticker"])
            ft = str(r["fuel_type"])
            seen_makers[(day, maker)] = ticker
            key = (day, maker, ticker, ft)
            totals[key] = totals.get(key, 0) + int(r.get("registrations") or 0)

        for (day, maker), ticker in seen_makers.items():
            for ft in ("EV", "ICE"):
                totals.setdefault((day, maker, ticker, ft), 0)

        out = [
            {
                "date": day,
                "maker": maker,
                "ticker": ticker,
                "fuel_type": ft,
                "registrations": reg,
            }
            for (day, maker, ticker, ft), reg in sorted(totals.items())
        ]
        return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="MoRTH Vahan daily registrations scraper")
    ap.add_argument("--date", default=None, help="Report date YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="Parse only; do not write CSV")
    args = ap.parse_args()
    scraper = VahanDashboardScraper()
    print(json.dumps(scraper.run(dry_run=args.dry_run, report_date=args.date), indent=2))
