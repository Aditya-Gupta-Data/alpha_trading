"""
src/ingestion/sandbox/deep_history.py — 20 years of prices, and a steel proxy
=============================================================================

V2 RESEARCH SANDBOX, 2026-08-16. Two blockers, one module.

**MAC-ONLY. NEVER RUN THIS ON THE VM.** Same boundary as
`scripts/fetch_sector_bars.py`: yfinance is a Mac-lane dependency, Yahoo
rate-limits datacentre IPs, and `src/` proper must stay free of a yfinance
import — which is why the import is function-local and this module lives in
the sandbox, off every execution path.

BLOCKER 1 — THE EL NIÑO WINDOW. The bhavcopy lake starts 2019-10, so a
monsoon study had one usable El Niño (2023) and died at the gate. Yahoo
carries `^CNXFMCG` to 2011 and ITC/HINDUNILVR to 2006, which is 15-20 years
— enough to actually run it.

⚠️ **AND IT IS STILL NOT MANY EVENTS.** Across 2006-2026 there are exactly
**three distinct El Niño monsoon YEARS** (2009, 2015, 2023). The JJA and
JAS seasons inside one year are the same monsoon seen twice, so counting
six "events" would be counting three. That is why this module runs the
study two ways and reports both:

  * the DATED study (`event_study_simulator.run_dates`) — comparable to
    every other study in the sandbox, and honest about its clustering;
  * a YEAR-LEVEL test — one observation per monsoon year, El Niño years
    against the rest. Fewer numbers, but they are independent, which the
    dated version's n is not.

BLOCKER 2 — STEEL. MCX lists only STEELREBAR (construction rebar), which
does not track the flat/HRC steel driving TATASTEEL or JSWSTEEL. The honest
proxies are equity-side and free:

    SLX   VanEck Steel ETF — a basket of global steel producers, the
          closest available free stand-in for global steel pricing
    MT    ArcelorMittal — the largest global flat-steel producer
    XME   SPDR Metals & Mining — broader, for cross-checking

**These are NOT steel prices.** They are equities that co-move with steel,
so they carry equity beta and will move on days steel does not. Recorded as
`asset_class: steel_proxy` precisely so nobody later mistakes an ETF close
for an HRC quote. They deliberately do NOT go into `cross_asset.py`: that
module is the Dhan door, and putting a Yahoo series through it would break
the one-market-data-door rule.

CLI
    python3 -m src.ingestion.sandbox.deep_history --fetch
    python3 -m src.ingestion.sandbox.deep_history --el-nino-study
"""
import csv
import json
import statistics
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LAKE = ROOT / "data" / "lake" / "macro"
ONI_PATH = LAKE / "ONI.csv"

# {yahoo symbol: (filename, asset_class)}
DEEP_SERIES = {
    "^CNXFMCG": ("NIFTY_FMCG_DEEP.csv", "india_sector_index"),
    "ITC.NS": ("ITC_DEEP.csv", "india_equity"),
    "HINDUNILVR.NS": ("HINDUNILVR_DEEP.csv", "india_equity"),
    "SLX": ("STEEL_PROXY_SLX.csv", "steel_proxy"),
    "MT": ("STEEL_PROXY_MT.csv", "steel_proxy"),
    "XME": ("METALS_PROXY_XME.csv", "metals_proxy"),
}

MONSOON_SEASONS = ("JJA", "JAS")
EL_NINO_THRESHOLD = 0.5
# The monsoon's economic effect shows up in the harvest and the festive
# quarter, not in the week after a sea-surface reading. Jul→Dec is the
# window the hypothesis is actually about.
STUDY_START_MONTH, STUDY_END_MONTH = 7, 12


def fetch_series(symbol: str, period: str = "20y", fetch_fn=None) -> list:
    """[(date, close)] oldest first, or []. Never raises."""
    if fetch_fn is not None:
        return fetch_fn(symbol)
    try:
        import warnings
        import yfinance as yf
        warnings.filterwarnings("ignore")
        h = yf.Ticker(symbol).history(period=period, interval="1d")
        return [(str(i)[:10], float(c)) for i, c in zip(h.index, h["Close"])
                if c == c]
    except Exception as exc:
        print(f"  (deep_history: {symbol} failed [{type(exc).__name__}])")
        return []


def save_series(rows: list, filename: str, lake=None) -> Path:
    p = Path(lake or LAKE) / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("date,close\n" + "\n".join(f"{d},{c}" for d, c in rows) + "\n")
    return p


def load_series(filename: str, lake=None) -> dict:
    """{date: close} from a saved deep series, or {}."""
    p = Path(lake or LAKE) / filename
    try:
        return {r["date"]: float(r["close"]) for r in csv.DictReader(p.open())
                if r.get("close")}
    except (OSError, ValueError, KeyError):
        return {}


def load_oni(path=None) -> list:
    try:
        return [{"date": r["date"], "season": r["season"],
                 "year": int(r["year"]), "anom": float(r["anom"])}
                for r in csv.DictReader(Path(path or ONI_PATH).open())]
    except (OSError, ValueError, KeyError):
        return []


def monsoon_years(oni: list, threshold: float = EL_NINO_THRESHOLD) -> dict:
    """{year: peak monsoon ONI} for every year we have a JJA/JAS reading.

    Peak, not mean: a monsoon is judged by how strong the anomaly GOT, and
    averaging JJA with JAS would blunt exactly the years that matter."""
    out = {}
    for r in oni:
        if r["season"] in MONSOON_SEASONS:
            out[r["year"]] = max(out.get(r["year"], -9.9), r["anom"])
    return out


def season_return(series: dict, year: int) -> float | None:
    """Close-to-close % over the monsoon-to-festive window (Jul→Dec).

    None when either end is missing — the window is not silently shortened
    to whatever data happens to exist, because a 3-month return and a
    6-month return are not the same measurement."""
    start = [d for d in series if f"{year}-{STUDY_START_MONTH:02d}" <= d[:7]
             <= f"{year}-{STUDY_START_MONTH:02d}"]
    end = [d for d in series if d[:7] == f"{year}-{STUDY_END_MONTH:02d}"]
    if not start or not end:
        return None
    a, b = series[min(start)], series[max(end)]
    if not a:
        return None
    return round((b / a - 1) * 100, 3)


def _stats(vals: list) -> dict:
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"n": 0, "median_pct": None, "hit_rate_pct": None}
    return {"n": len(vals),
            "median_pct": round(statistics.median(vals), 3),
            "hit_rate_pct": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
            "mean_pct": round(statistics.fmean(vals), 3),
            "worst_pct": min(vals), "best_pct": max(vals)}


def year_level_study(series_file: str, lake=None, oni_path=None,
                     threshold: float = EL_NINO_THRESHOLD) -> dict:
    """ONE observation per monsoon year — El Niño years vs the rest.

    This is the version with real independence. The dated study counts
    ticker-days; this counts monsoons, which is what the hypothesis is
    about. It will always have a small n, and that is the honest ceiling
    of the question, not a defect of the method."""
    series = load_series(series_file, lake)
    peaks = monsoon_years(load_oni(oni_path))
    if not series or not peaks:
        return {"series": series_file, "verdict": "no_data",
                "el_nino": _stats([]), "other": _stats([])}
    years = sorted({int(d[:4]) for d in series})
    rows = []
    for y in years:
        if y not in peaks:
            continue
        r = season_return(series, y)
        if r is None:
            continue
        rows.append({"year": y, "oni_peak": peaks[y],
                     "el_nino": peaks[y] >= threshold, "return_pct": r})
    nino = [r["return_pct"] for r in rows if r["el_nino"]]
    other = [r["return_pct"] for r in rows if not r["el_nino"]]
    s_n, s_o = _stats(nino), _stats(other)
    return {
        "series": series_file,
        "window": f"{STUDY_START_MONTH:02d}→{STUDY_END_MONTH:02d} (monsoon→festive)",
        "years_measured": len(rows),
        "el_nino": s_n, "other": s_o,
        "el_nino_years": [r["year"] for r in rows if r["el_nino"]],
        "median_gap_pct": (None if not (s_n["n"] and s_o["n"])
                           else round(s_n["median_pct"] - s_o["median_pct"], 3)),
        # THE gate that matters here. Three El Niño monsoons is three
        # observations however many ticker-days they generate elsewhere.
        "verdict": ("insufficient_independent_events" if s_n["n"] < 5
                    else "measured"),
        "rows": rows,
    }


def main(argv=None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="Deep price history + steel "
                                             "proxies (Mac-only research)")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--el-nino-study", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)

    if a.fetch:
        for sym, (fn, cls) in DEEP_SERIES.items():
            rows = fetch_series(sym)
            if not rows:
                print(f"  {sym:<14} FAILED")
                continue
            save_series(rows, fn)
            print(f"  {sym:<14} {len(rows):>5} rows  {rows[0][0]} → "
                  f"{rows[-1][0]}  [{cls}] -> {fn}")
    if a.el_nino_study:
        out = {}
        for fn in ("NIFTY_FMCG_DEEP.csv", "ITC_DEEP.csv",
                   "HINDUNILVR_DEEP.csv"):
            out[fn] = year_level_study(fn)
        if a.json:
            print(json.dumps(out, indent=2))
        else:
            for fn, r in out.items():
                if r.get("verdict") == "no_data":
                    print(f"{fn}: no data")
                    continue
                n, o = r["el_nino"], r["other"]
                print(f"\n{fn}  ({r['years_measured']} monsoon years, "
                      f"window {r['window']})")
                print(f"  El Niño years {r['el_nino_years']}: n={n['n']}  "
                      f"median {n['median_pct']}%  hit {n['hit_rate_pct']}%")
                print(f"  All other years:      n={o['n']}  "
                      f"median {o['median_pct']}%  hit {o['hit_rate_pct']}%")
                print(f"  median gap: {r['median_gap_pct']}%   "
                      f"VERDICT: {r['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
