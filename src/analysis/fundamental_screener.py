"""
src/analysis/fundamental_screener.py — the Darling screen (Dept 8, advisory)
============================================================================

Phase 1 of the owner's Darling Pipeline (directive 2026-07-19): mechanical
quant filter over EXCHANGE-FILED numbers (`ingestion/nse_results` lake,
primary source) that writes every passer — no top-N cap, owner rule — to
`data/darlings_queue.json`. The report_downloader listens to that queue
(Phase 2); the parallel Claude session deep-reads the condensed output
(Phase 3) and its conviction JSON tells us whether a darling is genuine.

Seam placement (review-#2 law, the one amendment to the directive as
issued): capture lives in Dept 1 (`nse_results`, `fundamental_parser`);
JUDGMENT lives here in Dept 8. `conviction.py` stays the single scoring
seam — where a yfinance capture exists its `fundamental_factor` is
reported as an enrichment column; it never becomes a second gatekeeper.

The v1 PASS rule — mechanical, documented, NULL-honest (a company missing
the data to prove a criterion FAILS INTO `insufficient_data`, never gets
a guessed pass):

  1. revenue up YoY        latest filed quarter vs the same quarter last
                           year (banks: interest earned stands in for net
                           sales)
  2. profitable streak     net profit > 0 in ALL of the latest 4 filed
                           quarters (consolidated preferred)
  3. EPS not shrinking     basic EPS latest >= same quarter last year

  FORENSIC TRUST GATE (where our citation-clean deep-read exists):
  score < 45 or >= 2 red flags -> OUT (a darling with cooked books is
  the exact mirage this pipeline exists to catch); 45-60 -> passes
  FLAGGED. The four Issue-19 contaminated originals are never read.

Output: data/darlings_queue.json
  {"as_of", "criteria", "tickers": [...],          <- downloader contract
   "passed": [{symbol, metrics, forensic}], "flagged": [...],
   "rejected": {symbol: reason}, "insufficient_data": [...]}

CLI:  python3 -m src.analysis.fundamental_screener [--dry-run]
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS_LAKE = ROOT / "data" / "lake" / "financial_results"
FORENSIC_LAKE = ROOT / "data" / "lake" / "fundamental_reports"
QUEUE_PATH = ROOT / "data" / "darlings_queue.json"

IST = timezone(timedelta(hours=5, minutes=30))
# Issue 19: hand-made benchmarks whose citations never verified — the
# screener must never trust them. (EMUDHRA has the clean FY26.v2.)
CONTAMINATED = {("AZAD", "FY25"), ("JWL", "FY25"), ("VEDL", "FY25"),
                ("EMUDHRA", "FY26")}
FORENSIC_MIN_SCORE = 45
FORENSIC_MAX_RED = 1
FLAG_BAND = (45, 60)


def _profit(p: dict):
    """Consolidated preferred, standalone fallback — NULL-honest."""
    return (p.get("net_profit_consolidated")
            if p.get("net_profit_consolidated") is not None
            else p.get("net_profit"))


def _topline(p: dict, is_bank: bool):
    if is_bank and p.get("interest_earned") is not None:
        return p.get("interest_earned")
    return p.get("net_sale")


def screen_metrics(capture: dict) -> dict:
    """One NSE-results capture -> the three v1 criteria, each True/False/
    None (None = the filings don't carry enough data to judge)."""
    periods = (capture or {}).get("periods") or []
    is_bank = bool((capture or {}).get("is_bank"))
    m = {"revenue_yoy": None, "profit_streak_4q": None,
         "eps_not_shrinking": None, "is_bank": is_bank}
    if len(periods) >= 5:
        t0, t4 = _topline(periods[0], is_bank), _topline(periods[4], is_bank)
        if t0 is not None and t4 not in (None, 0):
            m["revenue_yoy"] = round((t0 - t4) / abs(t4), 4)
        e0, e4 = periods[0].get("eps_basic"), periods[4].get("eps_basic")
        if e0 is not None and e4 is not None:
            m["eps_not_shrinking"] = e0 >= e4
    if len(periods) >= 4:
        profits = [_profit(p) for p in periods[:4]]
        if all(v is not None for v in profits):
            m["profit_streak_4q"] = all(v > 0 for v in profits)
    return m


def forensic_view(symbol: str, lake_dir=None) -> dict | None:
    """Newest citation-clean deep-read for a symbol, or None. Prefers a
    .v2 file over its superseded original (the Issue-18 precedent)."""
    root = Path(lake_dir) if lake_dir else FORENSIC_LAKE
    d = root / symbol
    if not d.is_dir():
        return None
    best = None
    for f in sorted(d.glob("*.json"), reverse=True):   # v2 sorts after
        fy = f.stem.replace(".v2", "")
        if (symbol, fy) in CONTAMINATED and not f.stem.endswith(".v2"):
            continue
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        score = data.get("conviction_score")
        if not isinstance(score, (int, float)):
            continue
        view = {"fiscal_year": fy, "score": score,
                "red_flags": len(data.get("red_flags") or [])
                + len(data.get("hidden_debt_flags") or [])}
        if best is None or fy > best["fiscal_year"]:
            best = view
    return best


def screen_one(symbol: str, capture: dict, lake_dir=None) -> dict:
    """-> {"symbol", "status": pass|flagged|rejected|insufficient_data,
    "reason", "metrics", "forensic"}"""
    m = screen_metrics(capture)
    forensic = forensic_view(symbol, lake_dir)
    out = {"symbol": symbol, "metrics": m, "forensic": forensic}

    criteria = (m["revenue_yoy"], m["profit_streak_4q"],
                m["eps_not_shrinking"])
    if any(c is None for c in criteria):
        return {**out, "status": "insufficient_data",
                "reason": "filings lack the data to judge all 3 criteria"}
    if not (m["revenue_yoy"] > 0 and m["profit_streak_4q"]
            and m["eps_not_shrinking"]):
        return {**out, "status": "rejected", "reason": "quant criteria"}

    if forensic is not None:
        if (forensic["score"] < FORENSIC_MIN_SCORE
                or forensic["red_flags"] > FORENSIC_MAX_RED):
            return {**out, "status": "rejected",
                    "reason": f"forensic gate (score {forensic['score']}, "
                              f"{forensic['red_flags']} red flags)"}
        if FLAG_BAND[0] <= forensic["score"] <= FLAG_BAND[1]:
            return {**out, "status": "flagged",
                    "reason": "forensic score in the caution band"}
    return {**out, "status": "pass", "reason": None}


def run(results_dir=None, lake_dir=None, queue_path=None,
        write: bool = True) -> dict:
    """Screen every captured symbol; write the queue. ALL passers +
    flagged go into `tickers` (a flagged darling still deserves its
    deep-read — the flag rides along for Phase 3 to weigh)."""
    root = Path(results_dir) if results_dir else RESULTS_LAKE
    outcomes = []
    for f in sorted(root.glob("*.json")) if root.is_dir() else []:
        try:
            capture = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        outcomes.append(screen_one(f.stem, capture, lake_dir))

    passed = [o for o in outcomes if o["status"] == "pass"]
    flagged = [o for o in outcomes if o["status"] == "flagged"]
    queue = {
        "as_of": datetime.now(IST).replace(tzinfo=None)
                                  .isoformat(timespec="seconds"),
        "criteria": "v1: revenue up YoY + 4q profit streak + EPS not "
                    "shrinking; forensic gate score>=45 & <2 red flags",
        "tickers": sorted(o["symbol"] for o in passed + flagged),
        "passed": passed,
        "flagged": flagged,
        "rejected": {o["symbol"]: o["reason"] for o in outcomes
                     if o["status"] == "rejected"},
        "insufficient_data": sorted(o["symbol"] for o in outcomes
                                    if o["status"] == "insufficient_data"),
        "screened": len(outcomes),
    }
    if write:
        path = Path(queue_path) if queue_path else QUEUE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(queue, indent=1))
        queue["queue_path"] = str(path)
    return queue


if __name__ == "__main__":
    import sys

    q = run(write="--dry-run" not in sys.argv)
    print(f"screened {q['screened']}: {len(q['passed'])} pass, "
          f"{len(q['flagged'])} flagged, {len(q['rejected'])} rejected, "
          f"{len(q['insufficient_data'])} insufficient")
    for o in q["passed"]:
        m = o["metrics"]
        fx = o["forensic"]
        print(f"  PASS {o['symbol']:14} rev {m['revenue_yoy']:+.1%}"
              + (f"  forensic {fx['score']}" if fx else "  (no deep-read)"))
    for o in q["flagged"]:
        print(f"  FLAG {o['symbol']:14} {o['reason']}")
