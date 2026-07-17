"""
src/ingestion/fundamental_parser.py — fundamental statements ingestion
======================================================================

Data-lake philosophy (docs/HOLY_GRAIL_PLAN.md): CAPTURE EVERYTHING NOW, build
the screening logic later. For each tracked equity this pulls the full annual
AND quarterly income statement, balance sheet, and cash-flow statement, plus
the whole company `info` ratio/summary block, from yfinance (Yahoo Finance) —
the one free source rich enough for structural fundamentals. Nothing is
filtered: if a line item exists in the source, it is stored.

Isolation (same rule as news_processor): this module imports NO core trading
code and touches NO trade state — it only writes its own lake dataset. It is
NOT wired into the engine / Regime Compass (a pure data-gathering operation).
yfinance is the ONLY network dependency; every seam is injectable
(`ticker_factory`) so the parser is fully testable offline.

Storage — one self-contained JSON per ticker per capture, organised by ticker
then statement then fiscal period:
    data/lake/fundamentals/<TICKER>/<capture_date>.json
    {
      "ticker", "captured_at", "source",
      "statements": {
         "income_annual":    {"<period>": {"<line item>": value, ...}, ...},
         "income_quarterly": {...}, "balance_annual": {...},
         "balance_quarterly": {...}, "cashflow_annual": {...},
         "cashflow_quarterly": {...}
      },
      "info": { ...full yfinance info dict, JSON-cleaned... },
      "key_metrics": { curated ratios lifted from info },
      "derived":     { YoY / QoQ growth computed NULL-honestly }
    }

Fail-open per ticker (one bad symbol never aborts the batch). NaN/NumPy/
Timestamp values are coerced to JSON-safe primitives; missing fields stay
absent (never a guessed zero).

CLI:  python3 -m src.ingestion.fundamental_parser [TICKER ...]
"""
import json
import math
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LAKE_FUNDAMENTALS = ROOT / "data" / "lake" / "fundamentals"

# statement label -> the yfinance Ticker attribute that returns its DataFrame
_STATEMENTS = {
    "income_annual": "financials",
    "income_quarterly": "quarterly_financials",
    "balance_annual": "balance_sheet",
    "balance_quarterly": "quarterly_balance_sheet",
    "cashflow_annual": "cashflow",
    "cashflow_quarterly": "quarterly_cashflow",
}

# curated ratio/summary fields lifted from `info` for quick review (the FULL
# info dict is stored too — this is only a convenience surface, not a filter)
_KEY_INFO_FIELDS = (
    "totalRevenue", "ebitda", "netIncomeToCommon", "trailingEps", "forwardEps",
    "grossMargins", "operatingMargins", "profitMargins", "ebitdaMargins",
    "returnOnEquity", "returnOnAssets", "totalDebt", "totalCash",
    "debtToEquity", "quickRatio", "currentRatio", "freeCashflow",
    "operatingCashflow", "revenueGrowth", "earningsGrowth",
    "earningsQuarterlyGrowth", "marketCap", "enterpriseValue", "trailingPE",
    "forwardPE", "priceToBook", "enterpriseToEbitda", "dividendYield",
    "payoutRatio", "bookValue", "revenuePerShare",
)


def _clean(v):
    """Coerce any yfinance/pandas/NumPy value into a JSON-safe primitive.
    NaN/inf -> None; Timestamp/datetime -> ISO; NumPy scalar -> python;
    containers recursed. Never raises."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, str):
        return v
    if isinstance(v, (datetime, date)):
        return v.isoformat()[:10] if not isinstance(v, datetime) else v.isoformat()
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    # NumPy / pandas scalar (Timestamp, int64, float64, …)
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()
        except Exception:
            return str(v)
    if hasattr(v, "item"):
        try:
            return _clean(v.item())
        except Exception:
            return str(v)
    return str(v)


def _df_to_periods(df) -> dict:
    """A yfinance statement DataFrame (index = line items, columns = period
    end-dates) -> {period_iso: {line_item: value}}. Empty/None -> {}."""
    if df is None or getattr(df, "empty", True):
        return {}
    out = {}
    for col in df.columns:
        period = _clean(col)
        col_key = period[:10] if isinstance(period, str) else str(period)
        cells = {}
        for idx in df.index:
            try:
                val = _clean(df.at[idx, col])
            except Exception:
                val = None
            if val is not None:
                cells[str(idx)] = val
        out[col_key] = cells
    return out


def _series(periods: dict, *labels):
    """[(period, value)] newest-first for the first matching line-item label
    present across a statement's periods. [] when none match."""
    for label in labels:
        pts = [(p, d[label]) for p, d in periods.items()
               if isinstance(d, dict) and d.get(label) is not None]
        if pts:
            return sorted(pts, key=lambda x: x[0], reverse=True)
    return []


def _pct(cur, prev):
    if cur is None or prev is None or prev == 0:
        return None
    return round((cur - prev) / abs(prev) * 100, 2)


def _derive_growth(statements: dict) -> dict:
    """YoY / QoQ growth for revenue & net income — computed only where the
    line items exist (NULL-honest, never a guessed 0)."""
    ia, iq = statements.get("income_annual", {}), statements.get("income_quarterly", {})
    rev_a = _series(ia, "Total Revenue", "Operating Revenue")
    ni_a = _series(ia, "Net Income", "Net Income Common Stockholders",
                   "Net Income From Continuing Operation Net Minority Interest")
    rev_q = _series(iq, "Total Revenue", "Operating Revenue")
    ni_q = _series(iq, "Net Income", "Net Income Common Stockholders",
                   "Net Income From Continuing Operation Net Minority Interest")
    d = {
        "revenue_yoy_pct": _pct(rev_a[0][1], rev_a[1][1]) if len(rev_a) >= 2 else None,
        "net_income_yoy_pct": _pct(ni_a[0][1], ni_a[1][1]) if len(ni_a) >= 2 else None,
        "revenue_qoq_pct": _pct(rev_q[0][1], rev_q[1][1]) if len(rev_q) >= 2 else None,
        "revenue_yoy_q_pct": _pct(rev_q[0][1], rev_q[4][1]) if len(rev_q) >= 5 else None,
        "net_income_qoq_pct": _pct(ni_q[0][1], ni_q[1][1]) if len(ni_q) >= 2 else None,
        "net_income_yoy_q_pct": _pct(ni_q[0][1], ni_q[4][1]) if len(ni_q) >= 5 else None,
        "latest_annual_period": rev_a[0][0] if rev_a else None,
        "latest_quarter_period": rev_q[0][0] if rev_q else None,
    }
    return d


def fetch_ticker(ticker: str, ticker_obj=None, ticker_factory=None) -> dict:
    """Full fundamental record for one ticker. `ticker_obj`/`ticker_factory`
    are injectable (offline tests). Fail-open: a failed statement or info
    read leaves that section empty, never raises."""
    if ticker_obj is None:
        if ticker_factory is None:
            import yfinance as yf
            ticker_factory = yf.Ticker
        ticker_obj = ticker_factory(ticker)

    statements = {}
    for label, attr in _STATEMENTS.items():
        try:
            statements[label] = _df_to_periods(getattr(ticker_obj, attr, None))
        except Exception as exc:
            statements[label] = {}
            print(f"  (fundamentals: {ticker} {label} read failed [{exc}])")

    info = {}
    try:
        info = _clean(dict(getattr(ticker_obj, "info", {}) or {}))
    except Exception as exc:
        print(f"  (fundamentals: {ticker} info read failed [{exc}])")

    key_metrics = {k: info[k] for k in _KEY_INFO_FIELDS if info.get(k) is not None}

    return {
        "ticker": ticker,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "source": "yfinance",
        "statements": statements,
        "info": info,
        "key_metrics": key_metrics,
        "derived": _derive_growth(statements),
    }


def write_record(record: dict, root=None) -> Path | None:
    """One ticker record -> data/lake/fundamentals/<TICKER>/<capture_date>.json
    (REPLACE per capture day = idempotent). Returns the path, or None on any
    write failure (logged, never raised)."""
    root = Path(root) if root is not None else LAKE_FUNDAMENTALS
    day = (record.get("captured_at") or datetime.now().isoformat())[:10]
    target = root / record["ticker"] / f"{day}.json"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(record, indent=2, ensure_ascii=False))
        return target
    except (OSError, TypeError, ValueError) as exc:
        print(f"  (fundamentals: write {target} failed [{exc}])")
        return None


def run(tickers, out_root=None, ticker_factory=None, pause: float = 1.0) -> dict:
    """Fetch + persist a batch. Fail-open per ticker; polite pause between
    Yahoo calls. Returns a summary."""
    written, failed = [], []
    for i, tk in enumerate(tickers):
        try:
            rec = fetch_ticker(tk, ticker_factory=ticker_factory)
            path = write_record(rec, root=out_root)
            n_items = sum(len(v) for st in rec["statements"].values() for v in st.values())
            (written if path else failed).append(tk)
            print(f"(fundamentals: {tk} — {len(rec['info'])} info fields, "
                  f"{n_items} statement cells, derived rev YoY "
                  f"{rec['derived'].get('revenue_yoy_pct')}% -> {path})")
        except Exception as exc:
            failed.append(tk)
            print(f"(fundamentals: {tk} FAILED [{exc}])")
        if pause and i < len(tickers) - 1:
            time.sleep(pause)
    return {"written": written, "failed": failed,
            "out_root": str(out_root or LAKE_FUNDAMENTALS)}


if __name__ == "__main__":
    import sys
    syms = sys.argv[1:] or ["RELIANCE.NS", "TCS.NS"]
    print(json.dumps(run(syms), indent=2))
