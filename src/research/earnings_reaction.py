# MANUAL OFFLINE TOOL — V2 R&D sandbox. No cron line, no live importer.
"""Earnings Reaction Study — the "scoreboard against reality".

Reads the quarterly filings lake (`data/lake/financial_results/<SYM>.json`,
NSE integrated-filing XBRL, Rs lakhs), computes YoY growth for every quarter
that has a same-quarter-prior-year twin in the same file, and feeds the
FILING dates into `event_study_simulator.run_dates`-style machinery
(same `forward_return`, same `_stats`, same MIN_SAMPLE gate) bucketed by
growth quartile.

Hypothesis (stated before the first run, 2026-08-16): top-quartile YoY
net-profit growth → positive median 5d/10d forward return; bottom quartile
→ negative.

Honesty notes that shape the code:
* `filed_on` carries a clock time. A filing at 11:00 is priced into that
  day's close, so the event date is moved to the PREVIOUS session; a
  filing after 15:30 keeps its own date. `forward_return` then measures
  sessions strictly after the event date either way.
* YoY growth is undefined when the base-year figure is <= 0 (a loss
  turning into a profit is not "+340%"). Those rows are dropped and
  counted, never imputed.
* Consolidated profit is preferred; standalone is the fallback and the
  choice is recorded per event.
* The lake's DEPTH is whatever it is — this module reports the span of
  filing dates it actually found and refuses to pretend a train/verify
  split exists when every event sits in one calendar year.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = ROOT / "data" / "lake" / "financial_results"
MARKET_CLOSE = (15, 30)
WINDOWS = (5, 10)


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _parse_to(s):
    """'31-MAR-2026' -> date, or None."""
    try:
        return datetime.strptime(str(s), "%d-%b-%Y").date()
    except ValueError:
        return None


def _parse_filed(s):
    """'09-Jul-2026 18:36:12' -> (date, after_close: bool) or None."""
    try:
        dt = datetime.strptime(str(s), "%d-%b-%Y %H:%M:%S")
    except ValueError:
        try:
            dt = datetime.strptime(str(s), "%d-%b-%Y")
            return dt.date(), True      # no clock: assume the usual post-close drop
        except ValueError:
            return None
    return dt.date(), (dt.hour, dt.minute) >= MARKET_CLOSE


def profit_of(p: dict):
    """(value, field) — consolidated first, standalone as fallback."""
    for k in ("net_profit_consolidated", "net_profit"):
        v = _num(p.get(k))
        if v is not None:
            return v, k
    return None, None


def revenue_of(p: dict):
    for k in ("net_sale", "total_income", "interest_earned"):
        v = _num(p.get(k))
        if v is not None:
            return v, k
    return None, None


def _yoy(cur, prior):
    if cur is None or prior is None or prior <= 0:
        return None
    return round((cur / prior - 1) * 100, 3)


def events_for(symbol: str, doc: dict) -> tuple[list, dict]:
    """Every quarter with a same-quarter twin one year earlier.

    Returns ([event], {skip_reason: count}). Event date is the session on
    or before which the filing was NOT yet priced (see module docstring)."""
    by_to = {}
    for p in doc.get("periods") or []:
        t = _parse_to(p.get("to"))
        if t:
            by_to[t] = p
    out, skipped = [], {}

    def skip(r):
        skipped[r] = skipped.get(r, 0) + 1

    for t, p in sorted(by_to.items()):
        twin = by_to.get(t.replace(year=t.year - 1))
        if twin is None:
            skip("no_prior_year_quarter"); continue
        filed = _parse_filed(p.get("filed_on"))
        if filed is None:
            skip("no_filed_on"); continue
        fdate, after_close = filed
        event_date = fdate if after_close else fdate - timedelta(days=1)
        cur, field = profit_of(p)
        prior, _ = profit_of(twin)
        pg = _yoy(cur, prior)
        if pg is None:
            skip("profit_yoy_undefined"); continue
        rg = _yoy(revenue_of(p)[0], revenue_of(twin)[0])
        out.append({
            "symbol": symbol.upper(), "quarter_to": t.isoformat(),
            "filed_on": p.get("filed_on"), "date": event_date.isoformat(),
            "filed_after_close": after_close, "profit_field": field,
            "profit_yoy_pct": pg, "revenue_yoy_pct": rg,
        })
    return out, skipped


def load_events(results_dir=None) -> tuple[list, dict]:
    d = Path(results_dir or RESULTS_DIR)
    events, skipped = [], {}
    for f in sorted(d.glob("*.json")):
        try:
            doc = json.loads(f.read_text())
        except (OSError, ValueError):
            skipped["unreadable_file"] = skipped.get("unreadable_file", 0) + 1
            continue
        ev, sk = events_for(doc.get("symbol") or f.stem, doc)
        events.extend(ev)
        for k, v in sk.items():
            skipped[k] = skipped.get(k, 0) + v
    return events, skipped


def quartiles(events: list, key: str = "profit_yoy_pct") -> dict:
    """{'Q1_top': [...], 'Q2': [...], 'Q3': [...], 'Q4_bottom': [...]}
    ranked on `key`, highest growth first. Ties broken by symbol so the
    split is deterministic."""
    ranked = sorted((e for e in events if e.get(key) is not None),
                    key=lambda e: (-e[key], e["symbol"]))
    n = len(ranked)
    q = max(1, n // 4)
    return {"Q1_top": ranked[:q], "Q2": ranked[q:2 * q],
            "Q3": ranked[2 * q:3 * q], "Q4_bottom": ranked[3 * q:]} if n else {}


def study(events: list, windows=WINDOWS, lake_dir=None, bhav_days: int = 3000,
          key: str = "profit_yoy_pct", label: str = "earnings_reaction") -> dict:
    """Median-first reaction stats per growth quartile. Same gates as the
    dated study: MIN_SAMPLE observations AND >=3 distinct event dates."""
    from src.research.event_study_simulator import (
        MIN_SAMPLE, _closes_by_symbol, _stats, forward_return)
    dates = sorted({e["date"] for e in events})
    rep = {"study": label, "rank_key": key, "windows": list(windows),
           "events": len(events), "distinct_event_dates": len(dates),
           "date_span": [dates[0], dates[-1]] if dates else None,
           "quartiles": {},
           "caveats": [
               "Survivorship uncorrected: only symbols still in the lake and "
               "still trading contribute; biases upward.",
               "MEAN IS NOT THE ANSWER — read median and hit-rate.",
               "Quartile membership is ranked WITHIN this sample; a 'top "
               "quartile' here is relative to peers in the same lake, not an "
               "absolute growth threshold.",
               "No p-values: windows overlap across a results season."]}
    if not events:
        rep["verdict"] = "no_events"
        return rep
    closes = _closes_by_symbol({e["symbol"] for e in events}, bhav_days,
                               lake_dir=lake_dir)
    rep["symbols_without_bars"] = sorted(
        {e["symbol"] for e in events if not closes.get(e["symbol"])})
    for qname, members in quartiles(events, key).items():
        qd = {e["date"] for e in members}
        block = {"n_events": len(members), "distinct_event_dates": len(qd),
                 "growth_range_pct": ([members[-1][key], members[0][key]]
                                      if members else None),
                 "results": {}}
        for w in windows:
            vals = [forward_return(closes.get(e["symbol"], {}), e["date"], w)
                    for e in members]
            s = _stats(vals)
            s["gate"] = ("too few distinct event dates" if len(qd) < 3 else
                         "n below MIN_SAMPLE" if s["n"] < MIN_SAMPLE else
                         "passed")
            s["verdict"] = "measured" if s["gate"] == "passed" else "insufficient_sample"
            block["results"][f"fwd_{w}d"] = s
        rep["quartiles"][qname] = block
    return rep


def render_lines(rep: dict) -> list:
    lines = [f"earnings reaction study — rank on {rep['rank_key']}",
             f"  {rep['events']} events over {rep['distinct_event_dates']} "
             f"filing dates {rep.get('date_span')}"]
    for q, b in (rep.get("quartiles") or {}).items():
        lo, hi = b["growth_range_pct"] or (None, None)
        lines.append(f"  {q:10s} n={b['n_events']} dates={b['distinct_event_dates']}"
                     f" growth {lo}..{hi}%")
        for w, s in b["results"].items():
            if s["n"] == 0:
                lines.append(f"     {w}: n=0"); continue
            lines.append(f"     {w}: n={s['n']} median={s['median_pct']:+.2f}% "
                         f"hit={s['hit_rate_pct']:.0f}% mean={s['mean_pct']:+.2f}%"
                         f"{' ⚠skew' if s['mean_median_diverge'] else ''} "
                         f"[{s['gate']}]")
    for c in rep.get("caveats", []):
        lines.append(f"  ! {c}")
    return lines


def main(argv=None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="start", default=None)
    ap.add_argument("--to", dest="end", default=None)
    ap.add_argument("--rank", default="profit_yoy_pct",
                    choices=["profit_yoy_pct", "revenue_yoy_pct"])
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    events, skipped = load_events()
    if a.start:
        events = [e for e in events if e["date"] >= a.start]
    if a.end:
        events = [e for e in events if e["date"] <= a.end]
    rep = study(events, key=a.rank)
    rep["skipped"] = skipped
    if a.json:
        print(json.dumps(rep, indent=1, default=str))
    else:
        for line in render_lines(rep):
            print(line)
        print(f"  skipped: {skipped}")
    return 0


if __name__ == "__main__":
    sys_exit = main()
    raise SystemExit(sys_exit)
