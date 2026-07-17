"""
src/analysis/macro_shocks.py — the Crisis-Regime ('War/Shock') Playbook
=======================================================================

SCAFFOLD (owner directive 2026-07-16): hardcode the date windows of major
geopolitical / macro shocks and map how each of our NSE sectors performed
during them, to learn a Crisis Regime Playbook — when VIX spikes on shock
news, which sectors to FAVOUR (defensive / beneficiary) vs AVOID (vulnerable).

Method (point-in-time honest): for each shock window, each sector index's
window return and max intra-window drawdown, plus its EXCESS return vs NIFTY 50
(outperformance during the crisis is the 'defensive' tell). Aggregated across
shocks -> a favoured / vulnerable ranking. Read-only; NOT wired into the engine.
Reads data/sector_index_bars.json + data/bars_cache.json (NIFTY/VIX).
"""
import json
import bisect
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SECTOR_BARS = ROOT / "data" / "sector_index_bars.json"
BARS_CACHE = ROOT / "data" / "bars_cache.json"

# Major shock windows (start, end) — peak-fear phase of each event.
SHOCKS = {
    "2014_Crimea":          ("2014-02-20", "2014-04-30"),
    "2019_Balakot":         ("2019-02-14", "2019-03-15"),
    "2020_COVID_crash":     ("2020-02-20", "2020-04-07"),
    "2022_Russia_Ukraine":  ("2022-02-24", "2022-04-30"),
    "2023_Israel_Hamas":    ("2023-10-07", "2023-11-30"),
}


def _load():
    sec = json.loads(SECTOR_BARS.read_text())
    mkt = json.loads(BARS_CACHE.read_text())
    nifty = {d: c for d, _l, _h, c in mkt["bars"]["NIFTY 50"]}
    return sec, nifty, mkt.get("vix", {})


def _slice(bars_dates, closes, start, end):
    lo = bisect.bisect_left(bars_dates, start)
    hi = bisect.bisect_right(bars_dates, end) - 1
    if lo > hi or lo >= len(closes):
        return None
    return closes[lo:hi + 1]


def _window_return(closes):
    return (closes[-1] / closes[0] - 1) if closes and closes[0] else None


def _max_drawdown(closes):
    peak = closes[0]; mdd = 0.0
    for c in closes:
        peak = max(peak, c)
        mdd = min(mdd, c / peak - 1)
    return mdd


def sector_performance(shock: str) -> dict:
    """Per-sector window return, drawdown, and excess vs NIFTY for one shock."""
    start, end = SHOCKS[shock]
    sec, nifty, vix = _load()
    ndates = sorted(nifty); nclose = [nifty[d] for d in ndates]
    nwin = _slice(ndates, nclose, start, end)
    nifty_ret = _window_return(nwin) if nwin else None
    vdates = sorted(vix)
    vwin = _slice(vdates, [vix[d] for d in vdates], start, end)
    out = {"shock": shock, "window": [start, end], "nifty_return_pct":
           round(nifty_ret * 100, 1) if nifty_ret is not None else None,
           "vix_peak": round(max(vwin), 1) if vwin else None, "sectors": {}}
    for sym, blob in sec.items():
        dts = [r[0] for r in blob["bars"]]; cl = [float(r[3]) for r in blob["bars"]]
        w = _slice(dts, cl, start, end)
        if not w:
            continue
        ret = _window_return(w)
        out["sectors"][blob["sector"]] = {
            "index": sym, "return_pct": round(ret * 100, 1) if ret is not None else None,
            "max_drawdown_pct": round(_max_drawdown(w) * 100, 1),
            "excess_vs_nifty_pct": round((ret - nifty_ret) * 100, 1)
            if (ret is not None and nifty_ret is not None) else None,
        }
    return out


def crisis_playbook() -> dict:
    """Aggregate excess-vs-NIFTY across all shocks -> favoured / vulnerable
    sector ranking (mean excess return during crises)."""
    per_shock = {s: sector_performance(s) for s in SHOCKS}
    agg = {}
    for s, rep in per_shock.items():
        for sector, d in rep["sectors"].items():
            if d["excess_vs_nifty_pct"] is not None:
                agg.setdefault(sector, []).append(d["excess_vs_nifty_pct"])
    ranking = sorted(((sector, round(sum(v) / len(v), 1), len(v))
                      for sector, v in agg.items()), key=lambda x: -x[1])
    favoured = [r[0] for r in ranking if r[1] > 0]
    vulnerable = [r[0] for r in ranking if r[1] <= 0]
    return {"per_shock": per_shock, "mean_excess_ranking": ranking,
            "favoured_in_crisis": favoured, "avoid_in_crisis": vulnerable}


def active_shock(as_of: str) -> str | None:
    """Which known shock window (if any) contains `as_of` — the engine's
    'crisis regime' switch for point-in-time use."""
    for s, (start, end) in SHOCKS.items():
        if start <= as_of <= end:
            return s
    return None


if __name__ == "__main__":
    pb = crisis_playbook()
    print("CRISIS PLAYBOOK — mean sector excess return vs NIFTY across shocks:")
    for sector, mean_ex, n in pb["mean_excess_ranking"]:
        print(f"  {sector:12s} {mean_ex:+6.1f}%  (across {n} shocks)")
    print("  FAVOUR:", pb["favoured_in_crisis"])
    print("  AVOID :", pb["avoid_in_crisis"])
