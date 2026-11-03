"""
src/ingestion/alt_data/rbi_statistics.py — RBI monthly Payment System Indicators (UPI and retail payments)
=========================================================================================================

Replaces the NPCI scraper (npci.org.in blocks non-browser clients). Source (verified live 2026-11-03):
  * `https://www.rbi.org.in/Scripts/PSIUserView.aspx` — index of monthly releases, newest first, each
    linking `PSIUserView.aspx?Id=<n>` ("Payment System Indicators - September 2026").
  * The release page itself carries "PART I - Payment System Indicators" as an HTML table: one row per
    system (`2.3 IMPS`, `2.6 UPI @`, `Total Retail Payments ...`), volume in lakh and value in ₹ crore for
    the financial year to date, the same month a year earlier, the previous month and the release month.
The PDF / XLSX copies on rbidocs.rbi.org.in sit behind a bot challenge and are never fetched.

Each release gives three monthly columns; all three are written, so a later release overwrites a revised
month. Rows outside Part I, FY-to-date totals and unparseable cells are skipped.

Output (`data/lake/alt_data/rbi_payment_systems.csv`, key month + metric):
  `month, metric, volume_lakh, value_crore, avg_ticket_inr, release, release_id`
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any, Callable

from src.ingestion.alt_data.base_scraper import BaseAltDataScraper
from src.ingestion.alt_data.vahan_dashboard import _HTMLTableExtractor

RBI_PSI_INDEX_URL = "https://www.rbi.org.in/Scripts/PSIUserView.aspx"
RBI_PSI_RELEASE_URL = "https://www.rbi.org.in/Scripts/PSIUserView.aspx?Id={id}"
DEFAULT_RELEASES_PER_RUN = 2

_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"), 1)}
_RELEASE_LINK_RE = re.compile(
    r"PSIUserView\.aspx\?Id=(\d+)[^>]*>\s*Payment System Indicators\s*-\s*([A-Za-z]+\s+\d{4})", re.I)
_METRIC_RE = re.compile(r"^(?:\d+(?:\.\d+)*\s+\S|Total\s)", re.I)


def month_key(label: str) -> str | None:
    """`'2025 September'` / `'September 2026'` -> `'2025-09'` / `'2026-09'`; None otherwise."""
    words = re.sub(r"\s+", " ", str(label or "")).strip().lower().split(" ")
    year = next((w for w in words if re.fullmatch(r"\d{4}", w)), None)
    month = next((_MONTHS[w] for w in words if w in _MONTHS), None)
    return f"{year}-{month:02d}" if year and month else None


def clean_metric(label: str) -> str:
    """`'2.6 UPI @'` -> `'2.6 UPI'`; footnote marks and trailing formula text removed."""
    text = re.sub(r"\s+", " ", str(label or "")).strip()
    text = re.sub(r"\s*\(\d[\d+\-.\s]*\)?\s*$", "", text)
    return re.sub(r"[\s@*#$^]+$", "", text).strip()


def _num(cell: Any) -> float | None:
    s = re.sub(r"[,\s]", "", str(cell or ""))
    try:
        return float(s)
    except ValueError:
        return None


def parse_release_index(page_html: str) -> list[dict[str, Any]]:
    """Index page -> [{id, release}] newest first (release = 'YYYY-MM')."""
    out, seen = [], set()
    for rid, label in _RELEASE_LINK_RE.findall(page_html or ""):
        key = month_key(label)
        if key and rid not in seen:
            seen.add(rid)
            out.append({"id": int(rid), "release": key})
    out.sort(key=lambda r: r["id"], reverse=True)
    return out


def parse_part_one(page_html: str) -> list[dict[str, Any]]:
    """Part I table -> [{month, metric, volume_lakh, value_crore}] for the three monthly columns."""
    extractor = _HTMLTableExtractor()
    extractor.feed(html.unescape(page_html or ""))
    table = next((t for t in extractor.tables if t and t[0] and t[0][0].upper().startswith("PART I ")), None)
    if table is None or len(table) < 5:
        return []
    periods, tails = table[2], table[3]
    if len(periods) < 8 or len(tails) < 2:
        return []
    labels = [periods[1], f"{periods[2]} {tails[0]}", f"{periods[3]} {tails[1]}"]
    months = [month_key(lb) for lb in labels]
    if not all(months):
        return []
    out: list[dict[str, Any]] = []
    for row in table[4:]:
        if len(row) < 9 or not _METRIC_RE.match(row[0].strip()):
            continue
        metric = clean_metric(row[0])
        for k, month in enumerate(months):
            vol, val = _num(row[2 + k]), _num(row[6 + k])
            if vol is None and val is None:
                continue
            out.append({"month": month, "metric": metric, "volume_lakh": vol, "value_crore": val})
    return out


class RbiPaymentSystemsScraper(BaseAltDataScraper):
    """RBI Payment System Indicators: monthly UPI / IMPS / retail / digital payment volume and value."""

    dataset_name = "rbi_payment_systems"
    default_columns = ("month", "metric", "volume_lakh", "value_crore", "avg_ticket_inr", "release", "release_id")
    primary_key_columns = ("month", "metric")

    def __init__(
        self,
        lake_dir: Path | str | None = None,
        log_path: Path | str | None = None,
        releases_per_run: int = DEFAULT_RELEASES_PER_RUN,
        fetch_fn: Callable[..., bytes | str] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(lake_dir=lake_dir, log_path=log_path, fetch_fn=fetch_fn, sleep_fn=sleep_fn, **kwargs)
        self.releases_per_run = max(1, int(releases_per_run))

    def fetch_raw(self, **kwargs: Any) -> list[dict[str, Any]]:
        index = self.request(RBI_PSI_INDEX_URL).decode("utf-8", errors="replace")
        releases = parse_release_index(index)
        if not releases:
            raise ValueError("RBI PSI index listed no releases")
        out = []
        # Oldest first, so the newest release's (revised) figures win the CSV merge.
        for rel in reversed(releases[: self.releases_per_run]):
            page = self.request(RBI_PSI_RELEASE_URL.format(id=rel["id"]), headers={"Referer": RBI_PSI_INDEX_URL})
            out.append({**rel, "html": page.decode("utf-8", errors="replace")})
        return out

    def parse(self, raw: Any, **kwargs: Any) -> list[dict[str, Any]]:
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for rel in raw or []:
            rows = parse_part_one(rel.get("html", ""))
            if not rows:
                self.log_warning("PSI-LAYOUT", f"release {rel.get('id')}: Part I table not found")
                continue
            for r in rows:
                vol, val = r["volume_lakh"], r["value_crore"]
                ticket = round(val * 1e7 / (vol * 1e5), 2) if vol and val else None
                merged[(r["month"], r["metric"])] = {
                    "month": r["month"],
                    "metric": r["metric"],
                    "volume_lakh": "" if vol is None else f"{vol:.2f}",
                    "value_crore": "" if val is None else f"{val:.2f}",
                    "avg_ticket_inr": "" if ticket is None else f"{ticket:.2f}",
                    "release": rel["release"],
                    "release_id": rel["id"],
                }
        return list(merged.values())


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="RBI Payment System Indicators scraper")
    ap.add_argument("--releases", type=int, default=DEFAULT_RELEASES_PER_RUN, help="Newest releases to read")
    ap.add_argument("--dry-run", action="store_true", help="Parse only; do not write CSV")
    args = ap.parse_args()
    scraper = RbiPaymentSystemsScraper(releases_per_run=args.releases)
    print(json.dumps(scraper.run(dry_run=args.dry_run), indent=2))
