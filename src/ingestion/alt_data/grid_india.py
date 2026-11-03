"""
src/ingestion/alt_data/grid_india.py — Grid-India / POSOCO / NPP Power Demand Scraper
=====================================================================================

Targets the POSOCO (Grid Controller of India) and National Power Portal (NPP)
daily power supply position dashboards (`https://npp.gov.in/public-dashboards` and
`https://grid-india.in/daily-psp-report/`) to extract:
  * Daily **National Peak Power Demand** in Gigawatts (`peak_demand_gw`), automatically
    converting Megawatt (`MW`) figures (`>= 1000` or tagged `MW`) to `GW`.
  * Regional demand & **Regional Load Drops** (`load_drop_pct` and `load_drop_flag`)
    across `National` and the 5 regional grids (`NR`, `WR`, `SR`, `ER`, `NER`).

Output (`data/lake/alt_data/grid_india_power.csv`):
  Columns: `date, region, peak_demand_gw, demand_met_gw, shortage_gw,
            load_drop_pct, load_drop_flag`
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

NPP_DASHBOARD_URL = "https://npp.gov.in/public-dashboards/merit-india"
GRID_INDIA_PSP_URL = "https://grid-india.in/daily-psp-report/"

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

LOAD_DROP_THRESHOLD_PCT = -2.0  # <= -2.0% drop vs baseline/prior or shortage > 2% flags a load drop


def normalize_grid_region(raw_region: Any) -> str | None:
    cleaned = re.sub(r"[\-\s]+", " ", str(raw_region or "")).strip().upper()
    if not cleaned:
        return None
    if cleaned in REGION_ALIASES:
        return REGION_ALIASES[cleaned]
    # Check longest aliases first so "NORTH EASTERN" matches before "EASTERN" or "NORTHERN"
    for alias, canon in sorted(REGION_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True):
        if alias in cleaned:
            return canon
    return None


def parse_power_gw(raw_val: Any, unit_hint: str = "") -> float | None:
    """Parse a power demand reading into Gigawatts (`GW`).
    Values in `MW` (either via `unit_hint='MW'`, `'MW'` in string, or numeric `>= 500.0`)
    are converted to `GW` via `/ 1000.0`."""
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


def _parse_float(raw_val: Any) -> float | None:
    if raw_val is None:
        return None
    s = re.sub(r"[^\d.\-]", "", str(raw_val))
    if not s or s in {"-", "."}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


class GridIndiaScraper(BaseAltDataScraper):
    """POSOCO / Grid-India / NPP daily National Peak Power Demand & Regional Load Drop scraper."""

    dataset_name = "grid_india_power"
    default_columns = (
        "date",
        "region",
        "peak_demand_gw",
        "demand_met_gw",
        "shortage_gw",
        "load_drop_pct",
        "load_drop_flag",
    )
    primary_key_columns = ("date", "region")

    def __init__(
        self,
        lake_dir: Path | str | None = None,
        log_path: Path | str | None = None,
        endpoint_url: str = NPP_DASHBOARD_URL,
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

    def fetch_raw(self, report_date: str | None = None, **kwargs: Any) -> str:
        url = self.endpoint_url
        if report_date:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}date={str(report_date)[:10]}"
        raw_bytes = self.request(
            url,
            method="GET",
            headers={"Referer": GRID_INDIA_PSP_URL},
        )
        return raw_bytes.decode("utf-8", errors="replace")

    def parse(
        self,
        raw: Any,
        report_date: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Parse POSOCO/NPP JSON or HTML table into National & Regional GW demand rows."""
        default_day = str(report_date or _dt.date.today().isoformat())[:10]
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

    def _build_record(
        self,
        region_raw: Any,
        peak_raw: Any = None,
        met_raw: Any = None,
        shortage_raw: Any = None,
        baseline_raw: Any = None,
        load_drop_pct_raw: Any = None,
        day: str = "",
        unit_hint: str = "",
    ) -> dict[str, Any] | None:
        region = normalize_grid_region(region_raw)
        if not region:
            return None

        met_gw = parse_power_gw(met_raw, unit_hint)
        peak_gw = parse_power_gw(peak_raw, unit_hint)
        shortage_gw = parse_power_gw(shortage_raw, unit_hint)

        if peak_gw is None and met_gw is not None:
            peak_gw = round(met_gw + (shortage_gw or 0.0), 3)
        if peak_gw is None:
            return None
        if met_gw is None:
            met_gw = round(max(0.0, peak_gw - (shortage_gw or 0.0)), 3)
        if shortage_gw is None:
            shortage_gw = round(max(0.0, peak_gw - met_gw), 3)

        drop_pct = _parse_float(load_drop_pct_raw)
        if drop_pct is None:
            base_gw = parse_power_gw(baseline_raw, unit_hint)
            if base_gw and base_gw > 0:
                drop_pct = round(((met_gw - base_gw) / base_gw) * 100.0, 2)
            elif peak_gw > 0:
                drop_pct = round((-shortage_gw / peak_gw) * 100.0, 2)
            else:
                drop_pct = 0.0

        drop_flag = bool(drop_pct <= LOAD_DROP_THRESHOLD_PCT)
        return {
            "date": day,
            "region": region,
            "peak_demand_gw": f"{peak_gw:.3f}",
            "demand_met_gw": f"{met_gw:.3f}",
            "shortage_gw": f"{shortage_gw:.3f}",
            "load_drop_pct": f"{drop_pct:.2f}",
            "load_drop_flag": "true" if drop_flag else "false",
        }

    def _parse_json_obj(self, doc: Any, default_day: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if isinstance(doc, dict):
            day = str(doc.get("date") or doc.get("report_date") or default_day)[:10]
            unit_hint = str(doc.get("unit") or "")

            # Top-level national summary if present
            nat_peak = (
                doc.get("national_peak_gw")
                or doc.get("national_peak_mw")
                or doc.get("peak_demand_gw")
                or doc.get("peak_demand_mw")
            )
            nat_met = (
                doc.get("national_demand_met_gw")
                or doc.get("national_demand_met_mw")
                or doc.get("demand_met_gw")
                or doc.get("demand_met_mw")
            )
            if nat_peak is not None or nat_met is not None:
                rec = self._build_record(
                    region_raw="National",
                    peak_raw=nat_peak,
                    met_raw=nat_met,
                    shortage_raw=doc.get("shortage_gw") or doc.get("shortage_mw"),
                    baseline_raw=doc.get("baseline_gw") or doc.get("prior_day_gw"),
                    load_drop_pct_raw=doc.get("load_drop_pct"),
                    day=day,
                    unit_hint="MW" if ("_mw" in " ".join(doc.keys()).lower()) else unit_hint,
                )
                if rec:
                    out.append(rec)

            regions = doc.get("regions") or doc.get("regional_load") or doc.get("data") or []
            if isinstance(regions, dict):
                for reg_key, reg_val in regions.items():
                    if isinstance(reg_val, dict):
                        rec = self._build_record(
                            region_raw=reg_key,
                            peak_raw=reg_val.get("peak_demand_gw") or reg_val.get("peak_demand_mw") or reg_val.get("peak"),
                            met_raw=reg_val.get("demand_met_gw") or reg_val.get("demand_met_mw") or reg_val.get("met"),
                            shortage_raw=reg_val.get("shortage_gw") or reg_val.get("shortage_mw") or reg_val.get("shortage"),
                            baseline_raw=reg_val.get("baseline_gw") or reg_val.get("baseline_mw") or reg_val.get("prior_gw"),
                            load_drop_pct_raw=reg_val.get("load_drop_pct") or reg_val.get("drop_pct"),
                            day=day,
                            unit_hint=str(reg_val.get("unit") or unit_hint),
                        )
                        if rec:
                            out.append(rec)
            elif isinstance(regions, list):
                for item in regions:
                    if not isinstance(item, dict):
                        continue
                    rec = self._build_record(
                        region_raw=item.get("region") or item.get("name"),
                        peak_raw=item.get("peak_demand_gw") or item.get("peak_demand_mw") or item.get("peak_demand"),
                        met_raw=item.get("demand_met_gw") or item.get("demand_met_mw") or item.get("demand_met"),
                        shortage_raw=item.get("shortage_gw") or item.get("shortage_mw") or item.get("shortage"),
                        baseline_raw=item.get("baseline_gw") or item.get("baseline_mw") or item.get("prior_demand"),
                        load_drop_pct_raw=item.get("load_drop_pct") or item.get("drop_pct"),
                        day=str(item.get("date") or day)[:10],
                        unit_hint=str(item.get("unit") or unit_hint),
                    )
                    if rec:
                        out.append(rec)
        elif isinstance(doc, list):
            for item in doc:
                out.extend(self._parse_json_obj(item, default_day))

        return self._SynthesizeNationalIfNeeded(out, default_day)

    def _parse_html_table(
        self, table: list[list[str]], default_day: str
    ) -> list[dict[str, Any]]:
        if len(table) < 2:
            return []
        headers = [h.upper() for h in table[0]]
        unit_hint = "MW" if any("MW" in h for h in headers) else ""

        def _col(keywords: tuple[str, ...]) -> int | None:
            for idx, h in enumerate(headers):
                if any(k in h for k in keywords):
                    return idx
            return None

        reg_col = _col(("REGION", "STATE/REGION", "GRID", "PARTICULARS"))
        peak_col = _col(("PEAK DEMAND", "MAX DEMAND", "DEMAND (MW)", "DEMAND (GW)", "REQUIREMENT"))
        met_col = _col(("DEMAND MET", "PEAK MET", "MET", "AVAILABILITY", "SUPPLIED"))
        short_col = _col(("SHORTAGE", "DEFICIT", "UNMET"))
        drop_col = _col(("LOAD DROP", "DROP %", "CHANGE %", "VARIATION"))
        base_col = _col(("PREVIOUS", "BASELINE", "PRIOR"))

        if reg_col is None or (peak_col is None and met_col is None):
            return []

        out: list[dict[str, Any]] = []
        for r in table[1:]:
            if reg_col >= len(r):
                continue
            rec = self._build_record(
                region_raw=r[reg_col],
                peak_raw=r[peak_col] if (peak_col is not None and peak_col < len(r)) else None,
                met_raw=r[met_col] if (met_col is not None and met_col < len(r)) else None,
                shortage_raw=r[short_col] if (short_col is not None and short_col < len(r)) else None,
                baseline_raw=r[base_col] if (base_col is not None and base_col < len(r)) else None,
                load_drop_pct_raw=r[drop_col] if (drop_col is not None and drop_col < len(r)) else None,
                day=default_day,
                unit_hint=unit_hint,
            )
            if rec:
                out.append(rec)
        return self._SynthesizeNationalIfNeeded(out, default_day)

    def _SynthesizeNationalIfNeeded(
        self, rows: list[dict[str, Any]], day: str
    ) -> list[dict[str, Any]]:
        """If regional rows (NR, WR, SR, ER, NER) are present without an explicit `National`
        summary row, compute the summed `National` peak demand row automatically."""
        if not rows:
            return []
        has_national = any(r["region"] == "National" for r in rows)
        if not has_national:
            reg_rows = [r for r in rows if r["region"] in {"NR", "WR", "SR", "ER", "NER"}]
            if reg_rows:
                tot_peak = sum(float(r["peak_demand_gw"]) for r in reg_rows)
                tot_met = sum(float(r["demand_met_gw"]) for r in reg_rows)
                tot_short = sum(float(r["shortage_gw"]) for r in reg_rows)
                nat = self._build_record(
                    region_raw="National",
                    peak_raw=tot_peak,
                    met_raw=tot_met,
                    shortage_raw=tot_short,
                    day=reg_rows[0]["date"] or day,
                    unit_hint="GW",
                )
                if nat:
                    rows = [nat] + rows
        return rows


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Grid-India / POSOCO daily power demand scraper")
    ap.add_argument("--date", default=None, help="Report date YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="Parse only; do not write CSV")
    args = ap.parse_args()
    scraper = GridIndiaScraper()
    print(json.dumps(scraper.run(dry_run=args.dry_run, report_date=args.date), indent=2))
