"""
src/ingestion/alt_data/npci_upi.py — NPCI UPI Retail Velocity Scraper
=====================================================================

Targets the National Payments Corporation of India (NPCI) public UPI statistics
pages (`https://www.npci.org.in/what-we-do/upi/upi-ecosystem-statistics` and
`https://www.npci.org.in/what-we-do/upi/product-statistics`) to extract daily
and monthly UPI Peer-to-Merchant (P2M) transaction volumes (in Millions), values
(in INR Crore), and Average Ticket Sizes (ATS in INR).

Output (`data/lake/alt_data/npci_upi.csv`):
  Columns: `period, granularity, p2m_volume_mn, p2m_value_cr,
            avg_ticket_size_inr, total_volume_mn, total_value_cr, p2m_share_pct`
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.ingestion.alt_data.base_scraper import BaseAltDataScraper

NPCI_ECOSYSTEM_URL = "https://www.npci.org.in/what-we-do/upi/upi-ecosystem-statistics"
NPCI_PRODUCT_STATS_URL = "https://www.npci.org.in/what-we-do/upi/product-statistics"


def _clean_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_float(raw: Any) -> Optional[float]:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    cleaned = re.sub(r"[^0-9.\-]", "", str(raw).replace(",", ""))
    if not cleaned or cleaned in ("-", ".", "-."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def compute_avg_ticket_size_inr(p2m_volume_mn: float, p2m_value_cr: float) -> float:
    """Compute UPI P2M Average Ticket Size (ATS) in INR.

    Since 1 Crore = 10^7 INR and 1 Million = 10^6 transactions:
        ATS (INR) = (Value in Cr * 10^7) / (Volume in Mn * 10^6)
                  = (Value in Cr * 10.0) / (Volume in Mn)
    """
    if p2m_volume_mn <= 0.0 or p2m_value_cr < 0.0:
        return 0.0
    return round((p2m_value_cr * 10.0) / p2m_volume_mn, 2)


class NpciUpiScraper(BaseAltDataScraper):
    """Scraper for NPCI UPI P2M retail velocity and ticket size statistics."""

    dataset_name = "npci_upi"
    ENDPOINT_URL = NPCI_ECOSYSTEM_URL
    PRODUCT_STATS_URL = NPCI_PRODUCT_STATS_URL

    default_columns: Tuple[str, ...] = (
        "period",
        "granularity",
        "p2m_volume_mn",
        "p2m_value_cr",
        "avg_ticket_size_inr",
        "total_volume_mn",
        "total_value_cr",
        "p2m_share_pct",
    )
    primary_key_columns: Tuple[str, ...] = ("period", "granularity")

    def __init__(
        self,
        lake_dir: Path | str | None = None,
        log_path: Path | str | None = None,
        endpoint_url: Optional[str] = None,
        report_date: Optional[str] = None,
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
        self.endpoint_url = endpoint_url or self.ENDPOINT_URL
        self.report_date = report_date or dt.date.today().isoformat()

    def fetch_raw(self, **kwargs: Any) -> str:
        """Fetch NPCI UPI Ecosystem Statistics page (with Product Stats fallback)."""
        try:
            raw_bytes = self.request(
                self.endpoint_url,
                method="GET",
                headers={"Referer": "https://www.npci.org.in/"},
            )
        except Exception:
            raw_bytes = self.request(
                self.PRODUCT_STATS_URL,
                method="GET",
                headers={"Referer": "https://www.npci.org.in/"},
            )
        return raw_bytes.decode("utf-8", errors="replace")

    def parse(
        self,
        raw: Any,
        report_date: Optional[str] = None,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """Parse NPCI UPI payload into standardized P2M velocity rows."""
        if raw is None:
            return []

        default_period = str(report_date or self.report_date).strip()
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")

        raw_records: List[Dict[str, Any]] = []
        if isinstance(raw, (dict, list)):
            raw_records = self._extract_json_records(raw)
        elif isinstance(raw, str):
            stripped = raw.strip()
            if not stripped:
                return []
            if stripped.startswith(("{", "[")):
                try:
                    raw_records = self._extract_json_records(json.loads(stripped))
                except Exception:
                    raw_records = []
            if not raw_records:
                raw_records = self._parse_html_tables(stripped)

        aggregated: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for rec in raw_records:
            period = str(
                rec.get("period")
                or rec.get("month")
                or rec.get("date")
                or default_period
            ).strip()
            granularity = self._infer_granularity(period, rec.get("granularity"))

            p2m_vol = _parse_float(
                rec.get("p2m_volume_mn")
                if rec.get("p2m_volume_mn") is not None
                else rec.get("p2m_vol_mn")
                if rec.get("p2m_vol_mn") is not None
                else rec.get("p2m_volume")
            )
            p2m_val = _parse_float(
                rec.get("p2m_value_cr")
                if rec.get("p2m_value_cr") is not None
                else rec.get("p2m_val_cr")
                if rec.get("p2m_val_cr") is not None
                else rec.get("p2m_value")
            )
            if p2m_vol is None or p2m_val is None or p2m_vol <= 0:
                continue

            total_vol = _parse_float(
                rec.get("total_volume_mn")
                if rec.get("total_volume_mn") is not None
                else rec.get("total_volume")
            )
            if total_vol is None or total_vol < p2m_vol:
                p2p_vol = _parse_float(rec.get("p2p_volume_mn") or rec.get("p2p_volume")) or 0.0
                total_vol = p2m_vol + p2p_vol if p2p_vol > 0 else p2m_vol

            total_val = _parse_float(
                rec.get("total_value_cr")
                if rec.get("total_value_cr") is not None
                else rec.get("total_value")
            )
            if total_val is None or total_val < p2m_val:
                p2p_val = _parse_float(rec.get("p2p_value_cr") or rec.get("p2p_value")) or 0.0
                total_val = p2m_val + p2p_val if p2p_val > 0 else p2m_val

            ats_override = _parse_float(rec.get("avg_ticket_size_inr") or rec.get("ats_inr"))
            ats = (
                round(ats_override, 2)
                if ats_override is not None and ats_override > 0
                else compute_avg_ticket_size_inr(p2m_vol, p2m_val)
            )
            p2m_share = round((p2m_vol / total_vol) * 100.0, 2) if total_vol > 0 else 100.0

            aggregated[(period, granularity)] = {
                "p2m_volume_mn": f"{p2m_vol:.2f}",
                "p2m_value_cr": f"{p2m_val:.2f}",
                "avg_ticket_size_inr": f"{ats:.2f}",
                "total_volume_mn": f"{total_vol:.2f}",
                "total_value_cr": f"{total_val:.2f}",
                "p2m_share_pct": f"{p2m_share:.2f}",
            }

        results: List[Dict[str, Any]] = []
        for (period, granularity), metrics in sorted(aggregated.items()):
            results.append(
                {
                    "period": period,
                    "granularity": granularity,
                    **metrics,
                }
            )
        return results

    @staticmethod
    def _infer_granularity(period: str, explicit: Any = None) -> str:
        if explicit:
            norm = str(explicit).strip().lower()
            if norm in ("daily", "day", "d"):
                return "daily"
            if norm in ("monthly", "month", "m"):
                return "monthly"
        if re.match(r"^\d{4}-\d{2}-\d{2}$", period) or re.match(r"^\d{2}[-/]\d{2}[-/]\d{4}$", period):
            return "daily"
        return "monthly"

    def _extract_json_records(self, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        if isinstance(payload, dict):
            for key in ("data", "records", "statistics", "upi_stats", "rows", "items"):
                val = payload.get(key)
                if isinstance(val, list):
                    return [r for r in val if isinstance(r, dict)]
            if any(k in payload for k in ("p2m_volume_mn", "p2m_volume", "p2m_vol_mn")):
                return [payload]
        return []

    def _parse_html_tables(self, html_text: str) -> List[Dict[str, Any]]:
        """Parse NPCI UPI ecosystem HTML tables (supports single or multi-row headers)."""
        records: List[Dict[str, Any]] = []
        tables = re.findall(r"<table[^>]*>(.*?)</table>", html_text, flags=re.I | re.S)
        for table_html in tables:
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, flags=re.I | re.S)
            if len(rows) < 2:
                continue

            header_rows: List[List[str]] = []
            data_start_idx = 0
            for idx, r in enumerate(rows):
                ths = [_clean_text(c).lower() for c in re.findall(r"<th[^>]*>(.*?)</th>", r, flags=re.I | re.S)]
                if ths:
                    header_rows.append(ths)
                    data_start_idx = idx + 1
                else:
                    break

            if not header_rows:
                first_cells = [
                    _clean_text(c).lower()
                    for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", rows[0], flags=re.I | re.S)
                ]
                header_rows = [first_cells]
                data_start_idx = 1

            headers = header_rows[-1]
            col_map = self._map_columns(headers)

            for row_html in rows[data_start_idx:]:
                cells = [
                    _clean_text(c)
                    for c in re.findall(r"<td[^>]*>(.*?)</td>", row_html, flags=re.I | re.S)
                ]
                if len(cells) < 3:
                    continue

                def _col(name: str) -> Optional[str]:
                    idx = col_map.get(name)
                    if idx is not None and idx < len(cells):
                        return cells[idx]
                    return None

                period = _col("period") or cells[0]
                if not period or period.lower() in ("month", "date", "period", "total", "sr. no.", "s.no"):
                    continue

                p2m_vol = _parse_float(_col("p2m_volume_mn"))
                p2m_val = _parse_float(_col("p2m_value_cr"))

                # Fallback for standard NPCI 7+ column layout:
                # [Month | Total Vol | Total Val | P2P Vol | P2P Val | P2M Vol | P2M Val | ...]
                if (p2m_vol is None or p2m_val is None) and len(cells) >= 7:
                    p2m_vol = p2m_vol if p2m_vol is not None else _parse_float(cells[5])
                    p2m_val = p2m_val if p2m_val is not None else _parse_float(cells[6])

                if p2m_vol is None or p2m_val is None:
                    continue

                total_vol = _parse_float(_col("total_volume_mn"))
                total_val = _parse_float(_col("total_value_cr"))
                if total_vol is None and len(cells) >= 7:
                    total_vol = _parse_float(cells[1])
                if total_val is None and len(cells) >= 7:
                    total_val = _parse_float(cells[2])

                records.append(
                    {
                        "period": period,
                        "p2m_volume_mn": p2m_vol,
                        "p2m_value_cr": p2m_val,
                        "total_volume_mn": total_vol,
                        "total_value_cr": total_val,
                        "avg_ticket_size_inr": _parse_float(_col("avg_ticket_size_inr")),
                    }
                )
        return records

    @staticmethod
    def _map_columns(headers: Sequence[str]) -> Dict[str, int]:
        col_map: Dict[str, int] = {}
        for idx, h in enumerate(headers):
            if any(k in h for k in ("month", "period", "date")) and "period" not in col_map:
                col_map["period"] = idx
            elif ("p2m" in h or "merchant" in h) and any(k in h for k in ("vol", "mn", "no", "count")):
                col_map["p2m_volume_mn"] = idx
            elif ("p2m" in h or "merchant" in h) and any(k in h for k in ("val", "amt", "cr", "amount")):
                col_map["p2m_value_cr"] = idx
            elif "total" in h and any(k in h for k in ("vol", "mn", "no", "count")):
                col_map["total_volume_mn"] = idx
            elif "total" in h and any(k in h for k in ("val", "amt", "cr", "amount")):
                col_map["total_value_cr"] = idx
            elif any(k in h for k in ("ticket", "ats", "average")):
                col_map["avg_ticket_size_inr"] = idx
        return col_map


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="NPCI UPI P2M retail velocity scraper")
    ap.add_argument("--date", default=None, help="Report date/period YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="Parse only; do not write CSV")
    args = ap.parse_args()
    scraper = NpciUpiScraper()
    print(json.dumps(scraper.run(dry_run=args.dry_run, report_date=args.date), indent=2))
