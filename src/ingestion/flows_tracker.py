"""
src/ingestion/flows_tracker.py — FII/DII daily cash flows (EOD, advisory)
=========================================================================

Phase 1 of docs/HOLY_GRAIL_PLAN.md, first sibling stream of the deals
tracker (decision #60's NSE hatch, extended by a logged decision). NSE
publishes the day's provisional FII/FPI and DII cash-market buy/sell/net
figures after close — the macro-est smart-money signal there is: the macro
matrix says what SHOULD move indices; these numbers say who IS moving
them. One row per day, trivially parseable, backfillable later from the
same shape.

Same discipline as every ingestion module: live fetch → hand-editable
local snapshot → "none"; advisory-only; fail-open; never a trade path.

Artifacts:
  * data/fii_dii_flows.json          today's normalized row (overwritten)
  * data/lake/flows/date=YYYY-MM-DD  the permanent per-day history
  * data/flows_snapshot.json         hand-editable fallback input

Cron: daily 19:35 IST. Manual check:  python3 -m src.ingestion.flows_tracker
"""

import json
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from src import lake
from src.ingestion.deals_tracker import (_NSE_HEADERS, _NSE_HOME, HTTP_TIMEOUT,
                                         _nse_opener, parse_report_date)

ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_PATH = ROOT / "data" / "fii_dii_flows.json"
SNAPSHOT_PATH = ROOT / "data" / "flows_snapshot.json"

_NSE_FLOWS_API = "https://www.nseindia.com/api/fiidiiTradeReact"


def _to_crore(value) -> float | None:
    """NSE serves rupee-crore figures as strings with commas."""
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def normalize_flows(rows: list) -> dict | None:
    """NSE's per-category rows -> one normalized day:
    {as_of, fii: {buy, sell, net}, dii: {buy, sell, net}} (₹ crore).
    Categories are matched loosely (FII/FPI spellings drift). Returns None
    when neither side parsed — never a guessed zero."""
    if not isinstance(rows, list):
        return None
    out, as_of = {}, None
    for row in rows:
        if not isinstance(row, dict):
            continue
        cat = str(row.get("category") or "").upper()
        side = ("fii" if ("FII" in cat or "FPI" in cat)
                else "dii" if "DII" in cat else None)
        if side is None:
            continue
        entry = {
            "buy": _to_crore(row.get("buyValue")),
            "sell": _to_crore(row.get("sellValue")),
            "net": _to_crore(row.get("netValue")),
        }
        if entry["net"] is None and None not in (entry["buy"], entry["sell"]):
            entry["net"] = round(entry["buy"] - entry["sell"], 2)
        out[side] = entry
        as_of = as_of or parse_report_date(row.get("date"))
    if not out:
        return None
    return {"as_of": as_of, "fii": out.get("fii"), "dii": out.get("dii")}


# NSE's API gateway 403s a JSON call whose Referer doesn't match the
# endpoint's OWNING page (ledger 2026-07-12: this module borrowed the
# deals page's Referer via _NSE_HEADERS and got 403'd where the deals
# pull succeeded from the same host minutes earlier). Own Referer here.
_FLOWS_HEADERS = dict(_NSE_HEADERS,
                      Referer="https://www.nseindia.com/reports/fii-dii")


def _fetch_nse_flows(timeout: int = HTTP_TIMEOUT):
    """Live path -> (rows, raw_bytes) or None. Never raises. The homepage
    warm-up is tolerated separately (deals_tracker discipline): a failed
    warm-up must never abort the API attempt itself."""
    opener = _nse_opener()
    try:
        opener.open(urllib.request.Request(_NSE_HOME, headers=_FLOWS_HEADERS),
                    timeout=timeout).read()
    except (urllib.error.URLError, ValueError, OSError, TimeoutError):
        pass  # cookies may already exist / API may still answer
    try:
        req = urllib.request.Request(_NSE_FLOWS_API, headers=_FLOWS_HEADERS)
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
        payload = json.loads(raw.decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError, TimeoutError) as exc:
        print(f"  (flows tracker: NSE fetch failed [{exc}] — "
              "falling open to the local snapshot)")
        return None
    return (payload, raw) if isinstance(payload, list) else None


def _load_snapshot(path=None) -> list:
    """Hand-editable fallback: a bare list of NSE-shaped category rows, or
    {"rows": [...]}. Missing/broken -> []."""
    path = Path(path) if path is not None else SNAPSHOT_PATH
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError):
        print(f"  (flows tracker: unreadable snapshot {path})")
        return []
    rows = raw.get("rows") if isinstance(raw, dict) else raw
    return rows if isinstance(rows, list) else []


def run(output_path=None, snapshot_path=None, lake_root=None,
        today: date = None, use_live: bool = True) -> dict:
    """Fetch, normalize, persist: today's row (overwritten JSON) + the
    permanent lake partition + the raw payload archive. Fail-open; returns
    the normalized dict (source-labeled). Never raises."""
    today = today or date.today()
    rows, raw, source = None, None, "none"
    if use_live:
        fetched = _fetch_nse_flows()
        if fetched is not None:
            rows, raw = fetched
            source = "nse"
    if rows is None:
        rows = _load_snapshot(snapshot_path)
        source = "snapshot" if rows else "none"

    normalized = normalize_flows(rows) or {"as_of": None, "fii": None,
                                           "dii": None}
    normalized["source"] = source
    normalized["as_of"] = normalized["as_of"] or today.isoformat()

    out = Path(output_path) if output_path is not None else OUTPUT_PATH
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(normalized, indent=2))
    except OSError as exc:
        print(f"  (flows tracker: could not write {out} [{exc}])")
    if source != "none":
        lake.write_partition("flows", normalized["as_of"], [normalized],
                             root=lake_root)
        if raw:
            lake.archive_blob("flows_raw", normalized["as_of"], "fiidii",
                              raw, ext="json", root=lake_root)
    fii_net = (normalized.get("fii") or {}).get("net")
    dii_net = (normalized.get("dii") or {}).get("net")
    print(f"(flows tracker: {normalized['as_of']} [{source}] — "
          f"FII net {fii_net if fii_net is not None else '?'} cr, "
          f"DII net {dii_net if dii_net is not None else '?'} cr)")
    return normalized


def load_flows(path=None) -> dict:
    """Reader for downstream consumers: the normalized day, or {} on any
    problem. Never raises."""
    path = Path(path) if path is not None else OUTPUT_PATH
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


if __name__ == "__main__":
    run()
