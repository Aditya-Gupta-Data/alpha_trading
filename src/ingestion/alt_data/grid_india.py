"""
src/ingestion/alt_data/grid_india.py — Grid-India daily Power Supply Position (PSP) scraper
=========================================================================================

Source (verified live 2026-11-03): the grid-india.in site is a JS app; its file API
`POST https://webapi.grid-india.in/api/v1/file` with `{"_source": "GRDW", "_type": "DAILY_PSP_REPORT"}`
lists the NLDC daily PSP workbooks (`Title_` = `DD.MM.YY_NLDC_PSP`, the day the data covers),
downloadable from `https://webcdn.grid-india.in/<FilePath>` as legacy `.xls` (read with `xlrd`).

Section A of the first sheet ("Power Supply Position at All India and Regional level") gives,
per region (NR, WR, SR, ER, NER, TOTAL → National): evening-peak demand met and shortage (MW),
energy met / hydro / wind / solar / energy shortage (MU), and the day's maximum demand met (MW).

Output (`data/lake/alt_data/grid_india_power.csv`, key date + region):
  `date, region, peak_demand_gw, demand_met_gw, shortage_gw, load_drop_pct, load_drop_flag,
   max_demand_met_gw, energy_met_mu, hydro_mu, wind_mu, solar_mu, energy_shortage_mu, source_file`
  peak_demand_gw = evening-peak demand met + shortage (the requirement at 19:00).
  load_drop_pct = day-on-day change in energy met; blank when the prior day is not on file.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any, Callable

from src.ingestion.alt_data.base_scraper import BaseAltDataScraper

FILE_API_URL = "https://webapi.grid-india.in/api/v1/file"
FILE_CDN_URL = "https://webcdn.grid-india.in/"
GRID_INDIA_PSP_URL = "https://grid-india.in/daily-psp-report/"
PSP_FILE_TYPE = "DAILY_PSP_REPORT"
DEFAULT_FILES_PER_RUN = 3

REGION_ALIASES: dict[str, str] = {
    "ALL INDIA": "National",
    "INDIA": "National",
    "NATIONAL": "National",
    "TOTAL": "National",
    "NORTHERN": "NR",
    "NORTHERN REGION": "NR",
    "NR": "NR",
    "WESTERN": "WR",
    "WESTERN REGION": "WR",
    "WR": "WR",
    "SOUTHERN": "SR",
    "SOUTHERN REGION": "SR",
    "SR": "SR",
    "EASTERN": "ER",
    "EASTERN REGION": "ER",
    "ER": "ER",
    "NORTH EASTERN": "NER",
    "NORTH-EASTERN": "NER",
    "NORTH EASTERN REGION": "NER",
    "NER": "NER",
}

LOAD_DROP_THRESHOLD_PCT = -2.0

# Section A row label (lowercased prefix) -> field
_SECTION_A_ROWS: tuple[tuple[str, str], ...] = (
    ("demand met (mw) during evening peak", "demand_met_mw"),
    ("shoratge (mw) during evening peak", "shortage_mw"),
    ("shortage (mw) during evening peak", "shortage_mw"),
    ("energy met", "energy_met_mu"),
    ("hydro gen", "hydro_mu"),
    ("wind gen", "wind_mu"),
    ("solar gen", "solar_mu"),
    ("energy shortage", "energy_shortage_mu"),
    ("maximum demand met during the day", "max_demand_met_mw"),
)

_TITLE_DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{2})_NLDC_PSP", re.I)


def normalize_grid_region(raw_region: Any) -> str | None:
    cleaned = re.sub(r"[\-\s]+", " ", str(raw_region or "")).strip().upper()
    if not cleaned:
        return None
    if cleaned in REGION_ALIASES:
        return REGION_ALIASES[cleaned]
    for alias, canon in sorted(REGION_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True):
        if alias in cleaned:
            return canon
    return None


def parse_power_gw(raw_val: Any, unit_hint: str = "") -> float | None:
    """Parse a power reading into GW; `MW`-tagged (or numeric >= 500) values are divided by 1000."""
    if raw_val is None:
        return None
    s = str(raw_val).strip()
    if not s:
        return None
    upper = (s + " " + str(unit_hint or "")).upper()
    cleaned = re.sub(r"[^\d.\-]", "", s)
    if not cleaned or cleaned in {"-", "."}:
        return None
    try:
        val = float(cleaned)
    except ValueError:
        return None
    if val < 0:
        return None
    if "MW" in upper and "GW" not in upper:
        val = val / 1000.0
    elif "GW" not in upper and val >= 500.0:
        val = val / 1000.0
    return round(val, 3)


def _num(raw: Any) -> float | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    s = re.sub(r"[^\d.\-]", "", str(raw or ""))
    if not s or s in {"-", "."}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def title_to_date(title: str) -> str | None:
    """`02.11.26_NLDC_PSP` -> `2026-11-02`; None when the title is not a PSP file name."""
    m = _TITLE_DATE_RE.match(str(title or "").strip())
    if not m:
        return None
    try:
        return _dt.date(2000 + int(m[3]), int(m[2]), int(m[1])).isoformat()
    except ValueError:
        return None


def xls_sheet_rows(data: bytes) -> list[list[Any]]:
    """First sheet of a legacy `.xls` workbook as a list of row value lists."""
    import xlrd  # noqa: PLC0415 — optional dependency, only the Mini PC needs it

    book = xlrd.open_workbook(file_contents=data)
    sheet = book.sheet_by_index(0)
    return [sheet.row_values(r) for r in range(sheet.nrows)]


def parse_psp_section_a(rows: list[list[Any]]) -> dict[str, dict[str, float | None]]:
    """Section A -> {region: {field: value}} for NR/WR/SR/ER/NER/National; {} when not found."""
    header_idx = None
    regions: list[tuple[int, str]] = []
    for i, row in enumerate(rows):
        cells = [str(c).strip() for c in row]
        found = [(j, normalize_grid_region(c)) for j, c in enumerate(cells) if c.upper() in REGION_ALIASES]
        canon = [r for _, r in found]
        if {"NR", "WR", "SR", "ER", "NER"}.issubset(canon):
            header_idx, regions = i, [(j, r) for j, r in found if r]
            break
    if header_idx is None:
        return {}
    out: dict[str, dict[str, float | None]] = {r: {} for _, r in regions}
    for row in rows[header_idx + 1: header_idx + 20]:
        label = str(row[0] if row else "").strip().lower()
        if label.startswith("b. ") or label.startswith("b."):
            break
        field = next((f for prefix, f in _SECTION_A_ROWS if label.startswith(prefix)), None)
        if field is None:
            continue
        for j, region in regions:
            if field not in out[region]:
                out[region][field] = _num(row[j]) if j < len(row) else None
    return out


def _fmt(v: float | None, nd: int = 3) -> str:
    return "" if v is None else f"{v:.{nd}f}"


class GridIndiaScraper(BaseAltDataScraper):
    """Grid-India NLDC daily PSP: national and regional demand, shortage and generation mix."""

    dataset_name = "grid_india_power"
    default_columns = (
        "date",
        "region",
        "peak_demand_gw",
        "demand_met_gw",
        "shortage_gw",
        "load_drop_pct",
        "load_drop_flag",
        "max_demand_met_gw",
        "energy_met_mu",
        "hydro_mu",
        "wind_mu",
        "solar_mu",
        "energy_shortage_mu",
        "source_file",
    )
    primary_key_columns = ("date", "region")

    def __init__(
        self,
        lake_dir: Path | str | None = None,
        log_path: Path | str | None = None,
        files_per_run: int = DEFAULT_FILES_PER_RUN,
        fetch_fn: Callable[..., bytes | str] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        xls_reader: Callable[[bytes], list[list[Any]]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(lake_dir=lake_dir, log_path=log_path, fetch_fn=fetch_fn, sleep_fn=sleep_fn, **kwargs)
        self.files_per_run = max(1, int(files_per_run))
        self._xls_reader = xls_reader or xls_sheet_rows

    def list_psp_files(self) -> list[dict[str, Any]]:
        """Newest-first PSP file entries `{date, title, path}` from the file API."""
        raw = self.request(
            FILE_API_URL,
            method="POST",
            data=json.dumps({"_source": "GRDW", "_type": PSP_FILE_TYPE}),
            headers={"Content-Type": "application/json", "Origin": "https://grid-india.in",
                     "Referer": GRID_INDIA_PSP_URL},
        )
        doc = json.loads(raw.decode("utf-8", errors="replace"))
        entries = []
        for item in doc.get("retData") or []:
            day = title_to_date(item.get("Title_", ""))
            path = str(item.get("FilePath") or "")
            if day and path.lower().endswith((".xls", ".xlsx")):
                entries.append({"date": day, "title": item.get("Title_"), "path": path})
        entries.sort(key=lambda e: e["date"], reverse=True)
        seen, unique = set(), []
        for e in entries:
            if e["date"] not in seen:
                seen.add(e["date"])
                unique.append(e)
        return unique

    def fetch_raw(self, report_date: str | None = None, **kwargs: Any) -> list[dict[str, Any]]:
        files = self.list_psp_files()
        if report_date:
            files = [f for f in files if f["date"] == str(report_date)[:10]]
        else:
            files = files[: self.files_per_run]
        out = []
        for f in files:
            data = self.request(FILE_CDN_URL + f["path"].lstrip("/"), headers={"Referer": GRID_INDIA_PSP_URL})
            out.append({**f, "rows": self._xls_reader(data)})
        return out

    def parse(self, raw: Any, **kwargs: Any) -> list[dict[str, Any]]:
        """Workbook rows -> one CSV row per region per day; load drop vs the prior day on file."""
        prior: dict[tuple[str, str], float] = {}
        for r in self.read_existing_rows():
            v = _num(r.get("energy_met_mu"))
            if v is not None:
                prior[(r.get("date", ""), r.get("region", ""))] = v

        parsed: list[tuple[str, str, dict[str, float | None], str]] = []
        for doc in raw or []:
            section = parse_psp_section_a(doc.get("rows") or [])
            if not section:
                self.log_warning("PSP-LAYOUT", f"{doc.get('title')}: Section A not found")
                continue
            for region, vals in section.items():
                parsed.append((doc["date"], region, vals, Path(doc.get("path", "")).name))
                if vals.get("energy_met_mu") is not None:
                    prior[(doc["date"], region)] = vals["energy_met_mu"]

        out: list[dict[str, Any]] = []
        for day, region, v, source in parsed:
            met_gw = None if v.get("demand_met_mw") is None else v["demand_met_mw"] / 1000.0
            short_gw = None if v.get("shortage_mw") is None else v["shortage_mw"] / 1000.0
            if met_gw is None:
                continue
            peak_gw = met_gw + (short_gw or 0.0)
            prev_day = (_dt.date.fromisoformat(day) - _dt.timedelta(days=1)).isoformat()
            prev, today = prior.get((prev_day, region)), v.get("energy_met_mu")
            drop = None if not prev or today is None else round((today - prev) / prev * 100.0, 2)
            max_mw = v.get("max_demand_met_mw")
            out.append({
                "date": day,
                "region": region,
                "peak_demand_gw": _fmt(peak_gw),
                "demand_met_gw": _fmt(met_gw),
                "shortage_gw": _fmt(short_gw),
                "load_drop_pct": _fmt(drop, 2),
                "load_drop_flag": "" if drop is None else ("true" if drop <= LOAD_DROP_THRESHOLD_PCT else "false"),
                "max_demand_met_gw": _fmt(None if max_mw is None else max_mw / 1000.0),
                "energy_met_mu": _fmt(v.get("energy_met_mu"), 2),
                "hydro_mu": _fmt(v.get("hydro_mu"), 2),
                "wind_mu": _fmt(v.get("wind_mu"), 2),
                "solar_mu": _fmt(v.get("solar_mu"), 2),
                "energy_shortage_mu": _fmt(v.get("energy_shortage_mu"), 2),
                "source_file": source,
            })
        return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Grid-India NLDC daily PSP scraper")
    ap.add_argument("--date", default=None, help="Data date YYYY-MM-DD (default: newest files)")
    ap.add_argument("--dry-run", action="store_true", help="Parse only; do not write CSV")
    args = ap.parse_args()
    scraper = GridIndiaScraper()
    print(json.dumps(scraper.run(dry_run=args.dry_run, report_date=args.date), indent=2))
