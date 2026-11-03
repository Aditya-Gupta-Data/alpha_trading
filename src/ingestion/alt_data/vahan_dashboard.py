"""
src/ingestion/alt_data/vahan_dashboard.py — MoRTH Vahan 4 registrations scraper (Maker x Fuel)
===========================================================================================

Source (verified live 2026-11-03): the public Vahan 4 dashboard report view, a PrimeFaces/JSF page.
  1. `GET reportview.xhtml` — session cookie + `javax.faces.ViewState`.
  2. AJAX refresh (`j_idt68`) with Y-axis = Maker, X-axis = Fuel, all states, calendar year = this year.
  3. The table's own Excel export (`groupingTable:xls`) — every maker (about 2,070) in one `.xlsx`.
Vahan publishes no daily series: the table is calendar-year-to-date. The scraper keeps one YTD snapshot
per night and turns consecutive snapshots into a per-day count.

Makers are folded onto listed companies with `MAKER_PATTERNS` (several legal entities per company, e.g.
Tata Motors + Tata Passenger Electric Mobility -> `Tata`); unmatched makers count only in `ALL`. Fuel
columns fold into `EV` / `ICE` with `classify_fuel_type` (`NOT APPLICABLE` and unknown fuels are dropped).

Outputs (`data/lake/alt_data/`):
  * `vahan_registrations_ytd.csv` — `date, year, maker, ticker, fuel_type, registrations_ytd`
  * `vahan_registrations.csv` — `date, maker, ticker, fuel_type, registrations, days_covered, prior_date`:
    registrations since the previous snapshot. No row until a prior snapshot exists, and none when the
    YTD figure went down (a Vahan correction), so a missing day is never written as zero.
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from src.ingestion.alt_data.base_scraper import IST, BaseAltDataScraper

VAHAN_BASE_URL = "https://vahan.parivahan.gov.in/vahan4dashboard/"
VAHAN_REPORT_URL = (
    "https://vahan.parivahan.gov.in/vahan4dashboard/vahan/view/reportview.xhtml"
)
VAHAN_ORIGIN = "https://vahan.parivahan.gov.in"
FORM_ID = "masterLayout_formlogin"
REFRESH_BUTTON = "j_idt68"
ALL_MAKERS = "ALL"
_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# Canonical Maker normalization: pattern -> (canonical_maker, nse_ticker)
MAKER_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\bTATA\b(?!.*\bHITACHI\b)", re.I), "Tata", "TATAMOTORS.NS"),
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
    "SOLAR",
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



def xlsx_sheet_rows(data: bytes) -> list[list[str]]:
    """First worksheet of an `.xlsx` as rows of strings (stdlib zip + XML; no openpyxl)."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{_XLSX_NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{_XLSX_NS}t")))
        sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    rows: list[list[str]] = []
    for row in sheet.iter(f"{_XLSX_NS}row"):
        cells: dict[int, str] = {}
        for c in row.iter(f"{_XLSX_NS}c"):
            col = re.match(r"[A-Z]+", c.get("r", "A")).group(0)
            idx = 0
            for ch in col:
                idx = idx * 26 + ord(ch) - 64
            v, kind = c.find(f"{_XLSX_NS}v"), c.get("t")
            if kind == "inlineStr":
                val = "".join(t.text or "" for t in c.iter(f"{_XLSX_NS}t"))
            elif v is None or v.text is None:
                val = ""
            elif kind == "s":
                val = shared[int(v.text)]
            else:
                val = v.text
            cells[idx - 1] = val
        rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
    return rows


def _clean(cell: Any) -> str:
    return re.sub(r"\s+", " ", str(cell or "").replace("\xa0", " ")).strip()


def _count(cell: Any) -> int | None:
    s = re.sub(r"[,\s]", "", _clean(cell))
    return int(s) if s.isdigit() else None


def parse_maker_fuel_rows(rows: list[list[Any]]) -> dict[tuple[str, str, str], int]:
    """Maker x Fuel YTD sheet -> {(maker, ticker, EV|ICE): count} for listed makers plus `ALL`."""
    fuel_row = next(
        (r for r in rows if sum(1 for c in r if classify_fuel_type(_clean(c))) >= 5), None)
    if fuel_row is None:
        return {}
    fuel_cols = {j: classify_fuel_type(_clean(c)) for j, c in enumerate(fuel_row)}
    fuel_cols = {j: f for j, f in fuel_cols.items() if f}
    out: dict[tuple[str, str, str], int] = {}
    for row in rows:
        if len(row) < 2 or not _clean(row[0]).isdigit():
            continue
        maker = normalize_maker(_clean(row[1]))
        for j, fuel in fuel_cols.items():
            n = _count(row[j]) if j < len(row) else None
            if not n:
                continue
            keys = [(ALL_MAKERS, "", fuel)]
            if maker:
                keys.append((maker[0], maker[1], fuel))
            for k in keys:
                out[k] = out.get(k, 0) + n
    return out


class VahanDashboardScraper(BaseAltDataScraper):
    """Vahan 4 Maker x Fuel YTD snapshot -> per-day registrations for listed auto makers."""

    dataset_name = "vahan_registrations"
    default_columns = ("date", "maker", "ticker", "fuel_type", "registrations", "days_covered", "prior_date")
    primary_key_columns = ("date", "maker", "fuel_type")
    snapshot_columns = ("date", "year", "maker", "ticker", "fuel_type", "registrations_ytd")

    def __init__(
        self,
        lake_dir: Path | str | None = None,
        log_path: Path | str | None = None,
        fetch_fn: Callable[..., bytes | str] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        today_fn: Callable[[], _dt.date] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(lake_dir=lake_dir, log_path=log_path, fetch_fn=fetch_fn, sleep_fn=sleep_fn, **kwargs)
        self._today = today_fn or (lambda: _dt.datetime.now(IST).date())

    @property
    def snapshot_path(self) -> Path:
        return self.lake_dir / f"{self.dataset_name}_ytd.csv"

    def _form(self, year: int) -> dict[str, str]:
        return {FORM_ID: FORM_ID, "j_idt29_input": "A", "j_idt38_input": "-1", "selectedRto_input": "-1",
                "yaxisVar_input": "Maker", "xaxisVar_input": "Fuel", "selectedYearType_input": "C",
                "selectedYear_input": str(year)}

    def fetch_raw(self, **kwargs: Any) -> dict[str, Any]:
        """Landing -> refresh -> export; returns the snapshot day and the export workbook bytes."""
        today = self._today()
        landing = self.request(VAHAN_REPORT_URL, headers={"Referer": VAHAN_BASE_URL}).decode("utf-8", errors="replace")
        viewstate = extract_viewstate(landing)
        if not viewstate:
            raise ValueError("Vahan report view carried no javax.faces.ViewState")
        refresh = dict(self._form(today.year), **{
            "javax.faces.partial.ajax": "true", "javax.faces.source": REFRESH_BUTTON,
            "javax.faces.partial.execute": "@all", "javax.faces.partial.render": "combTablePnl",
            REFRESH_BUTTON: REFRESH_BUTTON, "javax.faces.ViewState": viewstate})
        partial = self.request(VAHAN_REPORT_URL, method="POST", data=refresh, headers={
            "Faces-Request": "partial/ajax", "X-Requested-With": "XMLHttpRequest",
            "Referer": VAHAN_REPORT_URL, "Origin": VAHAN_ORIGIN}).decode("utf-8", errors="replace")
        if "groupingTable" not in partial:
            raise ValueError("Vahan refresh returned no report table")
        m = re.search(r"ViewState:0\"><!\[CDATA\[([^\]]+)\]\]>", partial)
        export = dict(self._form(today.year), **{"groupingTable:xls": "groupingTable:xls",
                                                 "javax.faces.ViewState": m.group(1) if m else viewstate})
        data = self.request(VAHAN_REPORT_URL, method="POST", data=export,
                            headers={"Referer": VAHAN_REPORT_URL, "Origin": VAHAN_ORIGIN})
        if not data.startswith(b"PK"):
            raise ValueError("Vahan export did not return an .xlsx workbook")
        return {"date": today.isoformat(), "year": today.year, "xlsx": data}

    def parse(self, raw: Any, **kwargs: Any) -> list[dict[str, Any]]:
        """Write tonight's YTD snapshot; return per-day rows diffed against the previous snapshot."""
        dry_run = getattr(self, "_dry_run", False)
        day, year = raw["date"], int(raw["year"])
        counts = parse_maker_fuel_rows(xlsx_sheet_rows(raw["xlsx"]))
        if not counts:
            self.log_warning("VAHAN-LAYOUT", "export sheet had no Maker x Fuel table")
            return []
        prior_rows = [r for r in self.read_existing_rows(self.snapshot_path) if r.get("date", "") < day]
        prior_day = max((r["date"] for r in prior_rows), default="")
        prior = {(r["maker"], r["fuel_type"]): r for r in prior_rows if r["date"] == prior_day}
        snapshot = [{"date": day, "year": year, "maker": mk, "ticker": tk, "fuel_type": fu,
                     "registrations_ytd": n} for (mk, tk, fu), n in sorted(counts.items())]
        self.write_csv_idempotent(snapshot, columns=self.snapshot_columns,
                                  pk_columns=("date", "maker", "fuel_type"), path=self.snapshot_path,
                                  dry_run=dry_run)
        if not prior_day:
            self.log_warning("VAHAN-FIRST-SNAPSHOT", f"{day}: no earlier snapshot, daily rows start tomorrow")
            return []
        days = (_dt.date.fromisoformat(day) - _dt.date.fromisoformat(prior_day)).days
        out: list[dict[str, Any]] = []
        for (mk, tk, fu), n in sorted(counts.items()):
            p = prior.get((mk, fu))
            if p is None:
                continue
            base = 0 if int(p.get("year") or 0) != year else int(p.get("registrations_ytd") or 0)
            delta = n - base
            if delta < 0:
                self.log_warning("VAHAN-YTD-DOWN", f"{mk} {fu}: {base} -> {n}")
                continue
            out.append({"date": day, "maker": mk, "ticker": tk, "fuel_type": fu, "registrations": delta,
                        "days_covered": days, "prior_date": prior_day})
        return out

    def run(self, dry_run: bool = False, **kwargs: Any) -> dict[str, Any]:
        """Same fail-open run; the dry-run flag also stops the snapshot write inside `parse`."""
        self._dry_run = bool(dry_run)
        return super().run(dry_run=dry_run, **kwargs)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Vahan 4 Maker x Fuel registrations scraper")
    ap.add_argument("--dry-run", action="store_true", help="Parse only; write neither CSV")
    args = ap.parse_args()
    scraper = VahanDashboardScraper()
    print(json.dumps(scraper.run(dry_run=args.dry_run), indent=2, default=str))
