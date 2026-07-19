"""
src/ingestion/integrated_results.py — FRESH quarterly results (Dept 1)
======================================================================

The Issue-21 fix: SEBI's integrated-filing regime (2025-) is where every
filing after ~Jan-2025 actually lives — the old results endpoints froze
because the data MOVED, not stopped. This clerk reads the new river:

  1. /api/integrated-filing-results?index=equities&symbol=X&period=Quarterly
     -> the filing list (verified live 2026-07-20: TCS's newest row is
     the 30-Jun-2026 quarter, broadcast 09-Jul-2026 — ten days old).
  2. Each row carries the filed XBRL XML on nsearchives — the structured
     primary document. We parse the in-capmkt tags with stdlib regex:
     RevenueFromOperations, ProfitLossForPeriod, BasicEarningsLossPer-
     ShareFromContinuingOperations, FaceValue/PaidUpValueOfEquityShare-
     Capital. XBRL values are RUPEES; stored as LAKHS (/1e5) to match
     the existing financial_results capture schema exactly, so the
     screener and valuation engine consume fresh data UNCHANGED.

Row selection per quarter (qe_Date): prefer Consolidated over
Standalone, newest creation_Date wins (revisions supersede originals).
Main-context assumption (documented): the primary statement's tags are
read at their FIRST occurrence sharing the revenue tag's contextRef —
validated against TCS's known Q1 FY27 numbers on build night.

Same house doctrine as every clerk: MAC-ONLY, polite (8-15s between
symbols on the www API; 2-4s between static archive XBRLs), one retry,
IR-4xx/5xx outage codes to logs/integrated_results.jsonl, per-symbol
fail-open, NULL-honest ('-'/missing -> None never 0).

Overwrites data/lake/financial_results/<SYM>.json with the fresh
periods (source field says so) — the stale results-comparision captures
this replaces are the Issue-21 corpse.

CLI:  python3 -m src.ingestion.integrated_results [--tickers ...]
      [--queue]   (the darlings queue's 91)   [--limit N]
"""
import json
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS_LAKE = ROOT / "data" / "lake" / "financial_results"
QUEUE_PATH = ROOT / "data" / "darlings_queue.json"
OUTAGE_LOG = ROOT / "logs" / "integrated_results.jsonl"

IST = timezone(timedelta(hours=5, minutes=30))
LIST_URL = ("https://www.nseindia.com/api/integrated-filing-results"
            "?index=equities&symbol={symbol}&period=Quarterly")
SYMBOL_THROTTLE = (8.0, 15.0)
XBRL_THROTTLE = (2.0, 4.0)
QUARTERS_NEEDED = 5

# in-capmkt tag -> (our field, divide-by for lakhs)
_TAGS = {
    "RevenueFromOperations": ("net_sale", 1e5),
    "ProfitLossForPeriod": ("net_profit", 1e5),
    "BasicEarningsLossPerShareFromContinuingOperations": ("eps_basic", 1.0),
    "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations":
        ("eps_basic_alt", 1.0),
    "FaceValueOfEquityShareCapital": ("face_value", 1.0),
    "PaidUpValueOfEquityShareCapital": ("paidup_capital", 1e5),
    "IncomeOnInvestments": ("interest_earned", 1e5),   # bank-ish fallback
}


def _now_iso() -> str:
    return datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds")


def _log_outage(symbol: str, code: str, detail: str, log_path=None) -> None:
    path = Path(log_path) if log_path else OUTAGE_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps({"ts": _now_iso(), "symbol": symbol,
                                 "code": code, "detail": detail[:300]}) + "\n")
    except OSError:
        pass


def _fetch_json(url: str):
    from src.ingestion.report_downloader import _fetch_json as f
    return f(url)


def _fetch_bytes(url: str) -> bytes:
    from src.ingestion.report_downloader import _fetch_bytes as f
    return f(url)


def parse_xbrl(xml: str) -> dict:
    """Filed XBRL -> our normalized fields (LAKHS), NULL-honest. Reads
    each tag's occurrence in the revenue tag's context when identifiable,
    else first occurrence."""
    out = {}
    anchor = re.search(
        r"<in-capmkt:RevenueFromOperations[^>]*contextRef=\"([^\"]+)\"",
        xml)
    ctx = anchor.group(1) if anchor else None
    for tag, (field, div) in _TAGS.items():
        pat = (rf"<in-capmkt:{tag}[^>]*contextRef=\"{re.escape(ctx)}\""
               rf"[^>]*>([^<]+)<") if ctx else None
        m = re.search(pat, xml) if pat else None
        if m is None:
            m = re.search(rf"<in-capmkt:{tag}[^>]*>([^<]+)<", xml)
        if m is None:
            continue
        raw = m.group(1).strip()
        try:
            out[field] = round(float(raw) / div, 4)
        except ValueError:
            continue
    if out.get("eps_basic") is None and "eps_basic_alt" in out:
        out["eps_basic"] = out.pop("eps_basic_alt")
    out.pop("eps_basic_alt", None)
    return out


def select_rows(listing) -> list:
    """Filing rows -> one row per quarter (newest first): financials
    type only, Consolidated preferred, newest creation_Date wins."""
    rows = listing if isinstance(listing, list) else \
        (listing or {}).get("data") or []
    by_quarter = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        if "Financial" not in str(r.get("type", "")):
            continue
        qe = str(r.get("qe_Date", "")).strip()
        if not qe or not r.get("xbrl"):
            continue
        cur = by_quarter.get(qe)
        def rank(row):
            return (str(row.get("consolidated", "")).startswith("Consol"),
                    str(row.get("creation_Date", "")))
        if cur is None or rank(r) > rank(cur):
            by_quarter[qe] = r

    def qdate(qe):
        try:
            return datetime.strptime(qe.title(), "%d-%b-%Y")
        except ValueError:
            return datetime.min
    return [by_quarter[q] for q in
            sorted(by_quarter, key=qdate, reverse=True)]


def fetch_one(symbol: str, fetch_json_fn=_fetch_json,
              fetch_bytes_fn=_fetch_bytes, out_dir=None, log_path=None,
              sleep_fn=time.sleep, quarters: int = QUARTERS_NEEDED) -> dict:
    """One symbol -> fresh capture in the lake. Never raises."""
    sym = symbol.split(".")[0].strip().upper()
    try:
        try:
            listing = fetch_json_fn(LIST_URL.format(symbol=sym))
        except Exception:
            sleep_fn(30.0)
            listing = fetch_json_fn(LIST_URL.format(symbol=sym))
        rows = select_rows(listing)[:quarters]
        if not rows:
            _log_outage(sym, "IR-404", "no integrated filings listed",
                        log_path)
            return {"symbol": sym, "status": "no_data"}
        periods, consolidated_any = [], False
        for r in rows:
            try:
                xml = fetch_bytes_fn(r["xbrl"]).decode("utf-8", "replace")
                fields = parse_xbrl(xml)
            except Exception as e:
                _log_outage(sym, "IR-500",
                            f"xbrl fetch/parse: {e}", log_path)
                fields = {}
            consolidated = str(r.get("consolidated", "")).startswith("Consol")
            consolidated_any |= consolidated
            periods.append({
                "from": None, "to": r.get("qe_Date"),
                "filed_on": r.get("broadcast_Date"),
                "net_sale": fields.get("net_sale"),
                "total_income": None,
                "pbt": None,
                "net_profit": (None if consolidated
                               else fields.get("net_profit")),
                "net_profit_consolidated": (fields.get("net_profit")
                                            if consolidated else None),
                "eps_basic": fields.get("eps_basic"),
                "debt_equity": None,
                "interest_earned": fields.get("interest_earned"),
                "gross_npa_pct": None, "capital_adequacy": None,
                "face_value": fields.get("face_value"),
                "paidup_capital": fields.get("paidup_capital"),
            })
            sleep_fn(random.uniform(*XBRL_THROTTLE))
        dest_root = Path(out_dir) if out_dir else RESULTS_LAKE
        dest = dest_root / f"{sym}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(
            {"symbol": sym, "captured_at": _now_iso(),
             "source": "nse-integrated-filing xbrl (filed, Rs lakhs)",
             "is_bank": all(p.get("net_sale") is None
                            and p.get("interest_earned") is not None
                            for p in periods) if periods else False,
             "periods": periods}, indent=1))
        return {"symbol": sym, "status": "captured",
                "quarters": len(periods),
                "newest": rows[0].get("qe_Date")}
    except Exception as e:
        code = "IR-401" if "401" in str(e) or "403" in str(e) else "IR-500"
        _log_outage(sym, code, f"{type(e).__name__}: {e}", log_path)
        return {"symbol": sym, "status": "outage", "code": code}


def run(symbols: list, **kw) -> dict:
    sleep_fn = kw.get("sleep_fn", time.sleep)
    results = []
    for i, sym in enumerate(symbols or []):
        results.append(fetch_one(sym, **kw))
        if i < len(symbols) - 1:
            sleep_fn(random.uniform(*SYMBOL_THROTTLE))
    by = {}
    for r in results:
        by[r["status"]] = by.get(r["status"], 0) + 1
    return {"as_of": _now_iso(), "attempted": len(results),
            "summary": by, "results": results}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=None)
    ap.add_argument("--queue", action="store_true",
                    help="the darlings queue's tickers")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    syms = args.tickers or []
    if args.queue:
        try:
            syms = json.loads(QUEUE_PATH.read_text()).get("tickers") or []
        except (OSError, ValueError):
            syms = []
    if args.limit:
        syms = syms[:args.limit]
    if not syms:
        print("nothing to fetch (--tickers or --queue)")
    else:
        out = run(syms)
        print(json.dumps({k: out[k] for k in ("as_of", "attempted",
                                              "summary")}, indent=2))
        for r in out["results"]:
            if r["status"] != "captured":
                print(f"  {r['symbol']:14} {r['status']} {r.get('code','')}")
