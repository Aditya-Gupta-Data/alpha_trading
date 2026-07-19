"""
src/ingestion/nse_results.py — exchange-filed financial results (Dept 1 clerk)
==============================================================================

The Darling Pipeline's grain supply (owner directive 2026-07-19): quarterly
results AS FILED WITH THE EXCHANGE, from NSE's results-comparison API — the
same JSON the site's own "Financial Results" tab renders. This is primary-
source truth (auditor-reviewed filings, not a third-party aggregator), which
is why it outranks yfinance in the screen and why no Screener.in ingestor
exists (owner ruling: manual exports break the loop; scraping a private
product breaks the house safe-crawl doctrine).

One call per symbol returns the last ~5 filed periods with the key lines:
net sales, total income, PBT, net profit, EPS, tax, D/E where filed, plus
bank-specific fields (NPA, capital adequacy) flagged via `is_bank`.
Amounts are in RUPEES LAKHS as filed; stored as floats, NULL-honest
(a line the company didn't file stays None — never a guessed zero).

House safe-crawl doctrine (the report_downloader/deals_tracker pattern):
certifi SSL, best-effort cookie warm-up, jittered 8-15s throttle between
symbols, one polite retry then an honest outage code, MAC-ONLY (the VM's IP
fronts the live engine). Fail-open per symbol; injectable fetch seam.

Storage: data/lake/financial_results/<SYMBOL>.json   (latest capture wins —
the API always returns the trailing window, so history accretes upstream)
Outages:  logs/nse_results.jsonl   NR-401 refused | NR-404 no data |
                                   NR-408 timeout | NR-500 unexpected

CLI (Mac):  python3 -m src.ingestion.nse_results --tickers TCS INFY
            python3 -m src.ingestion.nse_results --from-lake   (all forensic-
            lake tickers — the covered-universe sweep)
"""
import json
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS_LAKE = ROOT / "data" / "lake" / "financial_results"
FORENSIC_LAKE = ROOT / "data" / "lake" / "fundamental_reports"
OUTAGE_LOG = ROOT / "logs" / "nse_results.jsonl"

IST = timezone(timedelta(hours=5, minutes=30))
API_URL = "https://www.nseindia.com/api/results-comparision?symbol={symbol}"
THROTTLE_RANGE = (8.0, 15.0)
RETRY_PAUSE = 30.0

# filed-field -> our name; values arrive as strings-or-null in lakhs
_FIELDS = {
    "re_net_sale": "net_sale",
    "re_tot_inc": "total_income",
    "re_pro_loss_bef_tax": "pbt",
    "re_net_profit": "net_profit",
    "re_con_pro_loss": "net_profit_consolidated",
    "re_basic_eps_for_cont_dic_opr": "eps_basic",
    "re_debt_eqt_rat": "debt_equity",
    "re_int_earned": "interest_earned",       # banks file this, not net_sale
    "re_grs_npa_per": "gross_npa_pct",
    "re_cap_ade_rat": "capital_adequacy",
    # valuation_scorer needs shares outstanding: paid-up capital / face
    # value (both filed). Added 2026-07-19 (the P/S leg) — captures made
    # before this date lack them; the re-sweep refreshes the lake.
    "re_face_val": "face_value",
    "re_pdup": "paidup_capital",
}


def _now_iso() -> str:
    return datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds")


def _num(v):
    """Filed values are strings ('1183200', '32.71'), null, or '-'.
    NULL-honest: anything non-numeric stays None."""
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


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
    """Reuse the proven NSE session (certifi + best-effort warm-up)."""
    from src.ingestion.report_downloader import _fetch_json as rd_fetch
    return rd_fetch(url)


def normalize(raw: dict) -> dict:
    """API payload -> {"is_bank": bool, "periods": [ {from,to,filed_on,
    <_FIELDS values>}, ... ]} newest first, NULL-honest throughout.
    Shapeless input -> {"is_bank": False, "periods": []}."""
    rows = (raw or {}).get("resCmpData") or []
    is_bank = str((raw or {}).get("bankNonBnking", "")).upper() == "Y" or any(
        _num(r.get("re_int_earned")) is not None for r in rows
        if isinstance(r, dict))
    periods = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        p = {"from": r.get("re_from_dt"), "to": r.get("re_to_dt"),
             "filed_on": r.get("re_create_dt")}
        for src, dst in _FIELDS.items():
            p[dst] = _num(r.get(src))
        periods.append(p)
    return {"is_bank": is_bank, "periods": periods}


def fetch_one(symbol: str, fetch_json_fn=_fetch_json, out_dir=None,
              log_path=None, sleep_fn=time.sleep) -> dict:
    """One symbol -> lake JSON. Never raises. status:
    captured | no_data | outage."""
    sym = symbol.split(".")[0].strip().upper()
    try:
        try:
            raw = fetch_json_fn(API_URL.format(symbol=sym))
        except Exception:
            sleep_fn(RETRY_PAUSE)
            raw = fetch_json_fn(API_URL.format(symbol=sym))
        norm = normalize(raw)
        if not norm["periods"]:
            _log_outage(sym, "NR-404", "no filed periods in response",
                        log_path)
            return {"symbol": sym, "status": "no_data"}
        dest_root = Path(out_dir) if out_dir else RESULTS_LAKE
        dest = dest_root / f"{sym}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(
            {"symbol": sym, "captured_at": _now_iso(),
             "source": "nse-results-comparision (filed, Rs lakhs)",
             **norm}, indent=1))
        return {"symbol": sym, "status": "captured",
                "periods": len(norm["periods"]), "path": str(dest)}
    except TimeoutError as e:
        _log_outage(sym, "NR-408", str(e), log_path)
        return {"symbol": sym, "status": "outage", "code": "NR-408"}
    except Exception as e:
        code = "NR-401" if "401" in str(e) or "403" in str(e) else "NR-500"
        _log_outage(sym, code, f"{type(e).__name__}: {e}", log_path)
        return {"symbol": sym, "status": "outage", "code": code}


def run(symbols: list, fetch_json_fn=_fetch_json, out_dir=None,
        log_path=None, sleep_fn=time.sleep,
        throttle=THROTTLE_RANGE) -> dict:
    results = []
    for i, sym in enumerate(symbols or []):
        results.append(fetch_one(sym, fetch_json_fn, out_dir, log_path,
                                 sleep_fn))
        if i < len(symbols) - 1:
            sleep_fn(random.uniform(*throttle))
    by = {}
    for r in results:
        by[r["status"]] = by.get(r["status"], 0) + 1
    return {"as_of": _now_iso(), "attempted": len(results),
            "summary": by, "results": results}


def lake_tickers() -> list:
    """Every ticker with a forensic lake report — the covered universe."""
    if not FORENSIC_LAKE.is_dir():
        return []
    return sorted(p.name for p in FORENSIC_LAKE.iterdir()
                  if p.is_dir() and any(p.glob("*.json")))


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=None)
    ap.add_argument("--from-lake", action="store_true",
                    help="sweep every forensic-lake ticker")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    syms = args.tickers or (lake_tickers() if args.from_lake else [])
    if args.limit:
        syms = syms[:args.limit]
    if not syms:
        print("nothing to fetch (use --tickers or --from-lake)")
    else:
        out = run(syms)
        print(json.dumps({k: out[k] for k in ("as_of", "attempted",
                                              "summary")}, indent=2))
        for r in out["results"]:
            if r["status"] != "captured":
                print(f"  {r['symbol']:14} {r['status']} "
                      f"{r.get('code', '')}")
