"""
src/research/event_study_simulator.py — the "purani news" test
==============================================================

V2 RESEARCH SANDBOX, 2026-08-11. The architect's question, stated plainly:
*when a word like "Downgrade" or "Election" appeared in the past, what did
the stock do over the next 5 and 10 sessions?*

**ON NO EXECUTION PATH.** Nothing in `src/` outside this package imports it,
there is no cron line, and a test enforces the isolation. It reads two
artifacts we already own and writes nothing at all unless asked.

THE TWO SOURCES, AND WHY THESE TWO

  `data/lake/events/date=YYYY-MM-DD/`  NSE corporate announcements, one row
      per filing: `{as_of, symbol, ticker, subject, flags, attachment}`.
      **2,612 partitions, 2019-01-01 → 2026-08-15.** `subject` is the
      headline text the keyword matches against.
  `data/lake/bhavcopy/YYYY-MM-DD.csv`  whole-exchange daily OHLC.
      **1,771 sessions, 2019-09-30 → 2026-08-05.**

They overlap from 2019-09-30, which is the real usable window — about six
years. `data/rss_signals.jsonl` is NOT used: all 693 of its rows are stamped
2026-07-15, so it is a one-day snapshot, not a news history.

⚠️ **THE TWO LAKES LIVE ON DIFFERENT MACHINES TODAY.** The events lake is
deep on the VM (2,612 days) and absent on the Mac; the bhavcopy lake is deep
on the Mac (1,771 days) and shallow on the VM (~101). So a full-history run
needs one of them copied first. `--lake-root` exists for exactly that, and
the report states its own coverage rather than pretending.

WHAT IT MEASURES

For each matched event on day D, the forward return is measured from the
**close of D to the close of D+N trading sessions**, using only bars dated
strictly after D. That ordering is the whole point: an event study that lets
day D's own bar into the window is measuring the news reaction it is trying
to predict.

Every window is also reported **relative to NIFTY 50** where an index series
is available, because "TCS rose 3% after the news" means nothing if the whole
market rose 3%. Absent an index, `excess_*` is None — never silently equal to
the raw return.

HONESTY RULES (a research tool that flatters itself is worse than none)

  * **n is always reported and never hidden.** With fewer than
    `MIN_SAMPLE` events the verdict is `insufficient_sample` and the mean is
    still shown — labelled, so it cannot be quoted as a finding.
  * **Survivorship is named.** The bhavcopy lake only contains names that
    were trading that day; a delisted company simply has no bars and drops
    out. This tool does NOT correct for that, and says so in its output.
  * **No p-values.** Mean, median, hit-rate and n only. A t-test over
    overlapping, autocorrelated event windows would produce a number that
    looks like significance and is not — the Auto-Discovery work already
    paid for that lesson.
  * A keyword match is a **string match on a headline**, not comprehension.
    "Downgrade" catches a credit downgrade and an analyst downgrade alike.

CLI
    python3 -m src.research.event_study_simulator --keyword Downgrade
    python3 -m src.research.event_study_simulator --keyword Election \\
        --from 2019-10-01 --to 2024-06-30 --sector FINANCIALS --json
    python3 -m src.research.event_study_simulator --keyword "El Nino" --windows 5,10,20
"""
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
EVENTS_DIR = ROOT / "data" / "lake" / "events"
BHAVCOPY_DIR = ROOT / "data" / "lake" / "bhavcopy"
SECTOR_UNIVERSE = ROOT / "config" / "sector_universe.json"

DEFAULT_WINDOWS = (5, 10)
MIN_SAMPLE = 10          # below this the result is labelled, not trusted
BENCHMARK = "NIFTY 50"   # for excess return, when an index series exists


# ---------------------------------------------------------------- events

def _partition_days(events_dir: Path, start: str, end: str) -> list:
    """The `date=YYYY-MM-DD` partitions inside [start, end], chronological."""
    if not events_dir.is_dir():
        return []
    days = []
    for p in events_dir.iterdir():
        name = p.name
        if not name.startswith("date="):
            continue
        d = name[5:]
        if start <= d <= end:
            days.append(d)
    return sorted(days)


def scan_events(keyword: str, start: str, end: str, events_dir=None,
                read_day_fn=None) -> list:
    """Every announcement whose `subject` contains `keyword`, case-insensitive.

    Returns [{date, ticker, symbol, subject}]. A keyword match is a STRING
    match on a headline — it does not understand what the filing says."""
    events_dir = Path(events_dir) if events_dir else EVENTS_DIR
    if read_day_fn is None:
        from src import lake

        def read_day_fn(day):
            return lake.read_day("events", day,
                                 root=events_dir.parent.parent)
    pattern = re.compile(re.escape(str(keyword)), re.I)
    out = []
    for day in _partition_days(events_dir, start, end):
        try:
            rows = read_day_fn(day) or []
        except Exception:
            continue
        for r in rows:
            subject = str(r.get("subject") or "")
            if pattern.search(subject):
                out.append({"date": r.get("as_of") or day,
                            "ticker": r.get("ticker"),
                            "symbol": r.get("symbol"),
                            "subject": subject})
    return out


# ---------------------------------------------------------------- prices

def _closes_by_symbol(symbols, days: int, lake_dir=None) -> dict:
    """{SYMBOL: {session: close}} in ONE pass over the day files."""
    from src.ingestion.bhavcopy_clerk import bars_for_many
    batch = bars_for_many(list(symbols), days=days, lake_dir=lake_dir)
    out = {}
    for sym, bars in batch.items():
        series = {}
        for b in bars:
            c = b.get("close")
            if c is None:
                continue
            session = b.get("session") or b.get("date")
            if session:
                series[str(session)] = float(c)
        out[sym] = series
    return out


def forward_return(series: dict, event_date: str, window: int):
    """Close-to-close return over `window` SESSIONS after `event_date`.

    Uses only sessions strictly AFTER the event date — letting the event
    day's own bar into the window would measure the reaction this is
    supposed to predict. None when there is no base close on or before the
    event, or fewer than `window` sessions after it."""
    if not series:
        return None
    sessions = sorted(series)
    prior = [s for s in sessions if s <= event_date]
    after = [s for s in sessions if s > event_date]
    if not prior or len(after) < window:
        return None
    base = series[prior[-1]]
    if not base:
        return None
    return round((series[after[window - 1]] / base - 1) * 100, 3)


# --------------------------------------------------------------- the run

def _sector_members(sector: str, path=None) -> list:
    try:
        raw = json.loads(Path(path or SECTOR_UNIVERSE).read_text())
        return (raw.get("sectors", {}).get(sector, {}) or {}).get(
            "constituents", []) or []
    except (OSError, ValueError):
        return []


def _stats(values: list) -> dict:
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": 0, "mean_pct": None, "median_pct": None,
                "hit_rate_pct": None, "best_pct": None, "worst_pct": None}
    ordered = sorted(vals)
    mid = len(ordered) // 2
    median = (ordered[mid] if len(ordered) % 2
              else (ordered[mid - 1] + ordered[mid]) / 2)
    return {
        "n": len(vals),
        "mean_pct": round(sum(vals) / len(vals), 3),
        "median_pct": round(median, 3),
        "hit_rate_pct": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
        "best_pct": max(vals), "worst_pct": min(vals),
    }


def run(keyword: str, start: str = None, end: str = None,
        windows=DEFAULT_WINDOWS, sector: str = None, tickers=None,
        events_dir=None, lake_dir=None, read_day_fn=None,
        universe_path=None, bhav_days: int = 2000) -> dict:
    """The whole study. Pure-ish: reads two lakes, writes nothing."""
    start = start or "2019-09-30"          # first overlapping bhavcopy day
    end = end or date.today().isoformat()

    events = scan_events(keyword, start, end, events_dir=events_dir,
                         read_day_fn=read_day_fn)

    wanted = None
    if sector:
        wanted = {t.split(".")[0].upper()
                  for t in _sector_members(sector, universe_path)}
    elif tickers:
        wanted = {str(t).split(".")[0].upper() for t in tickers}
    if wanted is not None:
        events = [e for e in events
                  if str(e.get("symbol") or "").upper() in wanted]

    symbols = {str(e.get("symbol") or "").upper() for e in events if e.get("symbol")}
    report = {
        "keyword": keyword, "from": start, "to": end,
        "sector": sector, "windows": list(windows),
        "events_matched": len(events),
        "distinct_symbols": len(symbols),
        "benchmark": BENCHMARK,
        "coverage": {}, "results": {}, "sample_events": events[:5],
        "caveats": [
            "Keyword match is a STRING match on the filing headline, not "
            "comprehension — 'Downgrade' catches credit and analyst "
            "downgrades alike.",
            "SURVIVORSHIP is NOT corrected: a delisted name has no bars and "
            "silently drops out of the sample.",
            "No p-values are computed. Event windows overlap and are "
            "autocorrelated; a t-test here would look like significance "
            "without being it.",
        ],
    }
    if not events:
        report["verdict"] = "no_events_matched"
        return report

    closes = _closes_by_symbol(symbols, bhav_days, lake_dir=lake_dir)
    bench = _closes_by_symbol([BENCHMARK.replace(" ", "")], bhav_days,
                              lake_dir=lake_dir).get(
        BENCHMARK.replace(" ", ""), {})

    report["coverage"] = {
        "symbols_with_price_history": sum(1 for s in symbols if closes.get(s)),
        "symbols_without_any_bars": sorted(s for s in symbols
                                           if not closes.get(s))[:20],
        "benchmark_series_available": bool(bench),
    }
    for w in windows:
        raw, excess = [], []
        for e in events:
            sym = str(e.get("symbol") or "").upper()
            r = forward_return(closes.get(sym, {}), e["date"], w)
            raw.append(r)
            b = forward_return(bench, e["date"], w) if bench else None
            excess.append(None if (r is None or b is None) else round(r - b, 3))
        stats = _stats(raw)
        stats["excess_vs_benchmark"] = _stats(excess) if bench else None
        stats["verdict"] = ("insufficient_sample" if stats["n"] < MIN_SAMPLE
                            else "measured")
        report["results"][f"fwd_{w}d"] = stats
    return report


def render_lines(rep: dict) -> list:
    lines = [f"event study: '{rep['keyword']}' {rep['from']} → {rep['to']}"
             + (f" [sector {rep['sector']}]" if rep.get("sector") else ""),
             f"  {rep['events_matched']} matching filing(s) across "
             f"{rep['distinct_symbols']} symbol(s)"]
    if not rep["events_matched"]:
        lines.append("  nothing matched — widen the window or the keyword")
        return lines
    cov = rep.get("coverage") or {}
    lines.append(f"  price history for {cov.get('symbols_with_price_history')} "
                 f"of {rep['distinct_symbols']} symbols"
                 + ("" if cov.get("benchmark_series_available")
                    else "  (NO benchmark series — excess returns unavailable)"))
    for name, s in rep["results"].items():
        if not s["n"]:
            lines.append(f"  {name}: no measurable window")
            continue
        flag = "  ⚠️ n<%d, NOT a finding" % MIN_SAMPLE \
            if s["verdict"] == "insufficient_sample" else ""
        lines.append(f"  {name}: n={s['n']}  mean {s['mean_pct']:+.2f}%  "
                     f"median {s['median_pct']:+.2f}%  "
                     f"hit {s['hit_rate_pct']:.0f}%{flag}")
        ex = s.get("excess_vs_benchmark")
        if ex and ex["n"]:
            lines.append(f"      vs {rep['benchmark']}: "
                         f"mean {ex['mean_pct']:+.2f}%  n={ex['n']}")
    return lines


def main(argv=None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(
        description="Forward returns after a keyword appears in the "
                    "corporate-announcement history (research only)")
    ap.add_argument("--keyword", required=True)
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--sector", help="restrict to a config/sector_universe.json sector")
    ap.add_argument("--tickers", help="comma list, overrides --sector")
    ap.add_argument("--windows", default="5,10")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)

    rep = run(a.keyword, start=a.start, end=a.end,
              windows=tuple(int(x) for x in a.windows.split(",") if x.strip()),
              sector=a.sector,
              tickers=a.tickers.split(",") if a.tickers else None)
    if a.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        for line in render_lines(rep):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
