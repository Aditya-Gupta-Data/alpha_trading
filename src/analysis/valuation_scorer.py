"""
src/analysis/valuation_scorer.py — Valuation Normalization Engine (Dept 8)
==========================================================================

The owner's handoff spec (2026-07-19), implemented against what our
feeds can HONESTLY compute: Z-scores vs the universe -> weighted
composite -> sigmoid squeeze -> a 1-100 integer where 1 = deeply
undervalued, 100 = highly overvalued (spec directionality).

V1 metric set — the spec's five, reduced by data reality + owner
amendments, weights renormalized (documented, not hidden):

  P/E  0.40   close / TTM filed EPS          (spec w=0.25)
  PEG  0.30   P/E / EPS YoY growth %         (spec w=0.20)
  P/S  0.30   market cap / TTM filed revenue (stand-in for the P/B +
              FCF legs: book value and cash-flow aren't in the
              quarterly results feed; D/E dropped by owner amendment)

Everything is exchange-filed (nse_results lake) or our own bars
(bhavcopy lake). Shares outstanding derive from filed paid-up capital /
face value; mcap and revenue stay in Rs lakhs so P/S is unit-consistent.

Stats: winsorize each metric (drop top/bottom 1%) then mu/sigma —
macro-sector (config/sector_universe.json) when the sector has >=
MIN_SECTOR_N scored members, else market-wide over the captured
universe (the honest Nifty-500 stand-in). Spec's backtest & weight
optimization are Dept-5 work: the forward shadow record earns any
weight change, none is hand-tuned here.

Owner edge-case amendments (override the spec's fallbacks):
  * negative TTM EPS or non-positive growth -> instant VETO, never
    capped;
  * any metric uncomputable -> insufficient_data, never guessed.

Output: data/darlings_valuation.json — an ENRICHMENT beside the queue
(the queue file itself stays the downloader's untouched contract).
Advisory-only, MAC-ONLY (boundary doctrine — never scheduled on the VM).

CLI:  python3 -m src.analysis.valuation_scorer [--dry-run]
"""
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS_LAKE = ROOT / "data" / "lake" / "financial_results"
QUEUE_PATH = ROOT / "data" / "darlings_queue.json"
OUT_PATH = ROOT / "data" / "darlings_valuation.json"
SECTOR_MAP_PATH = ROOT / "config" / "sector_universe.json"

IST = timezone(timedelta(hours=5, minutes=30))
WEIGHTS = {"pe": 0.40, "peg": 0.30, "ps": 0.30}
MIN_SECTOR_N = 20
WINSOR_FRAC = 0.01


# ------------------------------------------------- per-symbol raw metrics

STALE_DAYS = 200          # newest filed quarter older than this -> unusable
EPS_STEP_MAX = 4.0        # a >4x jump inside 5 quarters = split/bonus scar


def _period_end(p: dict):
    """'31-DEC-2024' / '31-Dec-2024' -> date, or None."""
    from datetime import datetime as _dt
    for fmt in ("%d-%b-%Y", "%d-%B-%Y"):
        try:
            return _dt.strptime(str(p.get("to", "")).title(), fmt).date()
        except ValueError:
            continue
    return None


def ttm_metrics(capture: dict, close, today=None):
    """One results capture + latest close -> {"pe","peg","ps"} or a veto/
    insufficient marker. NULL-honest and veto-honest throughout.

    The BAJFINANCE lesson (2026-07-19 first live run): the API can serve
    a STALE trailing window (19 months old) and EPS series that straddle
    a split/bonus — summing those against today's price manufactures a
    fake-cheap P/E of 4. Two guards, both on already-stored data:
    staleness (newest quarter must be < STALE_DAYS old) and a
    corporate-action scar check (no >4x step inside the EPS series)."""
    periods = (capture or {}).get("periods") or []
    if close is None or len(periods) < 5:
        return {"status": "insufficient_data"}
    from datetime import date as _date, timedelta as _td
    end = _period_end(periods[0])
    today = today or _date.today()
    if end is None or (today - end) > _td(days=STALE_DAYS):
        return {"status": "insufficient_data",
                "reason": f"stale filings window (ends {periods[0].get('to')})"}
    eps = [p.get("eps_basic") for p in periods[:5]]
    if any(e is None for e in eps):
        return {"status": "insufficient_data"}
    pos = [abs(e) for e in eps if e]
    if pos and max(pos) / min(pos) > EPS_STEP_MAX:
        return {"status": "insufficient_data",
                "reason": "suspected corporate action in EPS series "
                          "(>4x step)"}
    ttm_eps = sum(eps[:4])
    if ttm_eps <= 0:
        return {"status": "veto", "reason": "negative TTM EPS"}
    growth_pct = ((eps[0] - eps[4]) / abs(eps[4]) * 100) if eps[4] else None
    if growth_pct is None or growth_pct <= 0:
        return {"status": "veto", "reason": "non-positive EPS growth"}
    pe = close / ttm_eps

    p0 = periods[0]
    face = p0.get("face_value")
    paidup = p0.get("paidup_capital")
    is_bank = bool(capture.get("is_bank"))
    tops = [(p.get("interest_earned") if is_bank else p.get("net_sale"))
            for p in periods[:4]]
    ps = None
    if face and paidup and all(t is not None for t in tops) and sum(tops) > 0:
        shares_lakh = paidup / face
        ps = (close * shares_lakh) / sum(tops)
    if ps is None:
        return {"status": "insufficient_data"}
    return {"status": "ok", "pe": round(pe, 2),
            "peg": round(pe / growth_pct, 3), "ps": round(ps, 3)}


# ------------------------------------------------------- universe stats

def winsorized_stats(values: list):
    """(mu, sigma) after dropping the top/bottom 1% (>=1 each side when
    n >= 20). None when fewer than 5 usable values — honest abstain."""
    vals = sorted(v for v in values if v is not None)
    if len(vals) < 5:
        return None
    trim = max(1, int(len(vals) * WINSOR_FRAC)) if len(vals) >= 20 else 0
    core = vals[trim:len(vals) - trim] if trim else vals
    mu = sum(core) / len(core)
    var = sum((v - mu) ** 2 for v in core) / len(core)
    return (mu, math.sqrt(var)) if var > 0 else None


def load_sector_map(path=None) -> dict:
    """{symbol: sector} from config/sector_universe.json — honest {} on
    any shape surprise."""
    try:
        raw = json.loads(Path(path or SECTOR_MAP_PATH).read_text())
        out = {}
        for sector, block in (raw or {}).items():
            names = block.get("constituents", block) if isinstance(
                block, dict) else block
            if isinstance(names, list):
                for t in names:
                    if isinstance(t, str):
                        out[t.split(".")[0].upper()] = sector
        return out
    except (OSError, ValueError, AttributeError):
        return {}


def build_stats(metrics_by_symbol: dict, sector_map: dict) -> dict:
    """{"market": {metric: (mu, sigma)}, "sectors": {sector: {...}}} —
    a sector earns its own stats only at >= MIN_SECTOR_N members."""
    stats = {"market": {}, "sectors": {}}
    ok = {s: m for s, m in metrics_by_symbol.items()
          if m.get("status") == "ok"}
    for metric in WEIGHTS:
        stats["market"][metric] = winsorized_stats(
            [m[metric] for m in ok.values()])
    by_sector = {}
    for sym, m in ok.items():
        sec = sector_map.get(sym)
        if sec:
            by_sector.setdefault(sec, []).append(m)
    for sec, rows in by_sector.items():
        if len(rows) >= MIN_SECTOR_N:
            stats["sectors"][sec] = {
                metric: winsorized_stats([r[metric] for r in rows])
                for metric in WEIGHTS}
    return stats


# ------------------------------------------------------------- the score

def score_one(metrics: dict, stats: dict, sector: str = None) -> dict:
    """Z per metric (sector stats when the sector earned them, else
    market) -> weighted composite -> sigmoid -> 1..100 int."""
    if metrics.get("status") != "ok":
        return dict(metrics)
    pool = stats["sectors"].get(sector, stats["market"])
    z, basis = {}, ("sector" if sector in stats["sectors"] else "market")
    for metric, w in WEIGHTS.items():
        st = pool.get(metric) or stats["market"].get(metric)
        if st is None:
            return {"status": "insufficient_data",
                    "reason": f"no universe stats for {metric}"}
        mu, sigma = st
        z[metric] = max(-3.0, min(3.0, (metrics[metric] - mu) / sigma))
    c = sum(WEIGHTS[m] * z[m] for m in WEIGHTS)
    score = round(1 + 99 / (1 + math.exp(-c)))
    return {"status": "ok", "score": max(1, min(100, score)),
            "composite_z": round(c, 3), "z": {k: round(v, 2)
                                              for k, v in z.items()},
            "metrics": {k: metrics[k] for k in WEIGHTS},
            "stats_basis": basis}


def run(results_dir=None, queue_path=None, out_path=None,
        closes: dict = None, sector_map: dict = None,
        write: bool = True) -> dict:
    """Universe metrics -> stats -> score every queued darling."""
    root = Path(results_dir) if results_dir else RESULTS_LAKE
    captures = {}
    for f in sorted(root.glob("*.json")) if root.is_dir() else []:
        try:
            captures[f.stem] = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
    if closes is None:
        from src.ingestion.bhavcopy_clerk import bars_for_many
        store = bars_for_many(list(captures), days=10)
        closes = {s: (b[-1]["close"] if b else None)
                  for s, b in store.items()}
    if sector_map is None:
        sector_map = load_sector_map()

    metrics = {s: ttm_metrics(c, closes.get(s))
               for s, c in captures.items()}
    stats = build_stats(metrics, sector_map)

    qp = Path(queue_path) if queue_path else QUEUE_PATH
    try:
        tickers = json.loads(qp.read_text()).get("tickers") or []
    except (OSError, ValueError):
        tickers = []

    scores, vetoed, insufficient = {}, {}, []
    for sym in tickers:
        r = score_one(metrics.get(sym, {"status": "insufficient_data"}),
                      stats, sector_map.get(sym))
        if r["status"] == "ok":
            scores[sym] = r
        elif r["status"] == "veto":
            vetoed[sym] = r.get("reason")
        else:
            insufficient.append(sym)

    out = {"as_of": datetime.now(IST).replace(tzinfo=None)
                                     .isoformat(timespec="seconds"),
           "method": "v1: z(PE .40, PEG .30, PS .30) vs winsorized "
                     "sector-or-market stats -> sigmoid 1..100 "
                     "(1=undervalued). P/B+FCF legs pending a "
                     "balance-sheet source; weights await Dept-5 "
                     "forward evidence.",
           "universe_n": sum(1 for m in metrics.values()
                             if m.get("status") == "ok"),
           "scores": scores, "vetoed": vetoed,
           "insufficient_data": sorted(insufficient),
           "advisory_note": "ADVISORY-ONLY (Law #63); Mac-only "
                            "(boundary doctrine)."}
    if write:
        op = Path(out_path) if out_path else OUT_PATH
        op.parent.mkdir(parents=True, exist_ok=True)
        op.write_text(json.dumps(out, indent=1))
        out["out_path"] = str(op)
    return out


if __name__ == "__main__":
    import sys

    o = run(write="--dry-run" not in sys.argv)
    ranked = sorted(o["scores"].items(), key=lambda kv: kv[1]["score"])
    print(f"valuation universe n={o['universe_n']} | scored "
          f"{len(o['scores'])} darlings, {len(o['vetoed'])} vetoed, "
          f"{len(o['insufficient_data'])} insufficient")
    for sym, r in ranked[:12]:
        print(f"  {r['score']:>3}  {sym:14} PE {r['metrics']['pe']:>7} "
              f"PEG {r['metrics']['peg']:>6} PS {r['metrics']['ps']:>6} "
              f"({r['stats_basis']})")
    if ranked:
        print("  ...")
        for sym, r in ranked[-3:]:
            print(f"  {r['score']:>3}  {sym:14} (most expensive)")
