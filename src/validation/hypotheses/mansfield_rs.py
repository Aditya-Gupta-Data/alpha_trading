"""Hypothesis B — MANSFIELD RELATIVE STRENGTH FILTER (decision #109).

"Equity desk entries filtered by Mansfield relative strength (outperforming
NIFTY) have a lower max drawdown than unfiltered entries."

    ratio_t = close_t / nifty_close_t
    MRS_t   = (ratio_t / SMA_N(ratio) − 1) × 100        N = 252 sessions (52w)
    MRS > 0  = outperforming the index at entry

Cohorts from the equity ledger's resolved entries (exit rows carry the
autopsy's r_multiple): `rs_filtered` (MRS > 0 at the entry date) vs
`unfiltered` (all). Metric: max drawdown of the cumulative-R path, entries
in date order. Bars come through the same by-id door the desk's trail uses
(`equity_trail.bars_for`); NIFTY from the Mac-shipped bars cache. A name
without bars is `unscored`, never guessed in or out. Not a live filter.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from ._metrics import summarize

ROOT = Path(__file__).resolve().parents[3]
BARS_CACHE = ROOT / "data" / "bars_cache.json"
NAME = "mansfield_rs"
DESCRIPTION = ("Mansfield RS: equity-desk entries outperforming NIFTY (MRS>0, 252d) "
               "have a lower max drawdown than unfiltered entries (equity ledger)")
DEFINITION = {"hypothesis": NAME, "benchmark": "NIFTY 50", "sma_sessions": 252,
              "filter": "mrs > 0 at entry", "metric": "max_drawdown_r",
              "cohorts": ["rs_filtered", "unfiltered"], "source": "equity_ledger.exits"}
SEAMS = ("events", "bars_fn", "nifty_closes_fn")
SMA_N = 252


def mansfield_rs(closes: list, index_closes: list, n: int = SMA_N) -> float | None:
    """MRS on the LAST bar of two aligned close series (same dates, oldest
    first). None below n + 1 aligned bars."""
    m = min(len(closes or []), len(index_closes or []))
    if m < n + 1:
        return None
    c, i = [float(x) for x in closes[-m:]], [float(x) for x in index_closes[-m:]]
    if any(v <= 0 for v in c) or any(v <= 0 for v in i):
        return None
    ratio = [a / b for a, b in zip(c, i)]
    sma = sum(ratio[-n:]) / n
    return round((ratio[-1] / sma - 1) * 100.0, 4)


def _nifty_closes_default() -> dict:
    """{date: close} for NIFTY 50 from the bars cache (Mac-shipped)."""
    try:
        bars = json.loads(BARS_CACHE.read_text()).get("bars", {}).get("NIFTY 50") or []
        return {b[0]: float(b[3]) for b in bars if len(b) >= 4}
    except (OSError, ValueError, TypeError):
        return {}


def rs_at_entry(ticker: str, entry_date: str, bars_fn=None, nifty: dict = None,
                n: int = SMA_N) -> float | None:
    """MRS on the entry date, from the darling's daily bars (by scrip id)
    aligned to NIFTY by date."""
    nifty = _nifty_closes_default() if nifty is None else nifty
    try:
        if bars_fn is None:
            from src.equity_desk import security_id_for
            from src.equity_trail import bars_for
            sid = security_id_for(ticker)
            if not sid:
                return None
            start = (date.fromisoformat(entry_date[:10]) - timedelta(days=int(n * 1.6) + 30)).isoformat()
            bars = bars_for(sid, start, today=date.fromisoformat(entry_date[:10]))
            bars = [b for b in bars if b[0] <= entry_date[:10]]
        else:
            bars = [b for b in (bars_fn(ticker, entry_date) or []) if b[0] <= entry_date[:10]]
    except Exception:
        return None
    aligned = [(b[3], nifty[b[0]]) for b in bars if b[0] in nifty]
    if len(aligned) < n + 1:
        return None
    return mansfield_rs([a for a, _ in aligned], [b for _, b in aligned], n)


def cohorts(events: list, bars_fn=None, nifty: dict = None) -> dict:
    """Resolved equity-ledger trades → {'rs_filtered': [R], 'unfiltered': [R],
    'unscored': n} in entry-date order."""
    entries = {e.get("id"): e for e in events or [] if e.get("event") == "entry"}
    rows = []
    for x in events or []:
        if x.get("event") != "exit":
            continue
        e = entries.get(x.get("id"))
        r = (x.get("kya_sikha_autopsy") or {}).get("r_multiple")
        if not e or r is None:
            continue
        rows.append((str(e.get("as_of") or "")[:10], e.get("ticker"), float(r), e))
    rows.sort(key=lambda t: t[0])
    filtered, unfiltered, unscored = [], [], 0
    for as_of, ticker, r, e in rows:
        unfiltered.append(r)
        mrs = rs_at_entry(ticker, as_of, bars_fn=bars_fn, nifty=nifty)
        if mrs is None:
            unscored += 1
        elif mrs > 0:
            filtered.append(r)
    return {"rs_filtered": filtered, "unfiltered": unfiltered, "unscored": unscored}


def evidence(events: list = None, bars_fn=None, nifty_closes_fn=None,
             today: date = None, min_n: int = 20) -> dict:
    if events is None:
        from src import knowledge_graph_logger as kg
        events = kg.read_events()
    nifty = nifty_closes_fn() if nifty_closes_fn else None
    c = cohorts(events, bars_fn=bars_fn, nifty=nifty)
    a, b = summarize(c["rs_filtered"]), summarize(c["unfiltered"])
    if a["n"] < min_n or b["n"] < min_n:
        verdict = "insufficient_n"
    elif a["max_drawdown_r"] is None or b["max_drawdown_r"] is None:
        verdict = "unmeasurable"
    else:
        verdict = "supports" if a["max_drawdown_r"] < b["max_drawdown_r"] else "contradicts"
    return {"verdict": verdict, "min_n": min_n, "rs_filtered": a, "unfiltered": b,
            "unscored": c["unscored"],
            "headline": (f"RS-filtered n={a['n']} maxDD {a['max_drawdown_r']}R vs unfiltered "
                         f"n={b['n']} maxDD {b['max_drawdown_r']}R")}
