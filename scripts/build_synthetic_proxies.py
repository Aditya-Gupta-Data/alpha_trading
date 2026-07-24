#!/usr/bin/env python3
"""
scripts/build_synthetic_proxies.py — Proxy Validation Protocol (owner directive 2026-07-24)
===========================================================================================

Deep-past episodes (1997/2000/2001) predate even the "deep" official sector
indices (NIFTY IT 2002, Pharma 2005, Auto 2004), so no sector leg can be priced
there. The workaround: SYNTHETIC PROXY indices from the legacy stalwarts that
defined each sector, back to 1995 — but a proxy built from today's survivors
must EARN trust via out-of-sample fidelity, not be granted it.

ITERATION 2 (owner directive 2026-07-24): the equal-weight 3-stock v1 failed
the 0.90 bar (IT 0.71 / Pharma 0.86 / Auto 0.78) — too concentrated to track a
diversified, cap-weighted index. Two honest fixes, both LOOK-AHEAD-FREE:

  * Constituent expansion — 5-8 legacy stalwarts per sector (point-in-time: a
    name contributes only from its own listing date; yfinance returns nothing
    before it, so no anachronistic membership).
  * Weighting — we DELIBERATELY DO NOT attempt true cap-weighting. yfinance
    exposes only CURRENT shares outstanding (a scalar), so historical market
    cap = 1995 price x today's shares would inject massive look-ahead bias
    (the Risk Manager's flag). Instead we test LIQUIDITY WEIGHTING: each name
    weighted by its TRAILING-63d average traded value (adj close x volume),
    LAGGED one day — a genuine size/prominence proxy computed purely from data
    known at the time. Split factors cancel in adj_close x adj_volume, so
    turnover is split-consistent. Compared head-to-head with expanded
    equal-weight to see whether weighting or diversification crosses 0.90.

Validation (unchanged): Pearson r of DAILY RETURNS + annualized tracking error
vs the OFFICIAL NIFTY sector index over their overlap. Dynamic Guardrail #3:
r >= 0.90 => the proxy is PREFER-eligible in the deep past; else SHOW-only.

Requires yfinance + pandas (Mac-lane utility, not a VM dep).
CLI: python3 scripts/build_synthetic_proxies.py [--threshold 0.90]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analysis import macro_features as MF

# sector lake KEY -> 5-8 legacy stalwarts (owner-provided core + expansion),
# Yahoo .NS tickers. Point-in-time membership is automatic (late listers
# simply contribute nothing before their listing).
CONSTITUENTS = {
    "NIFTY_IT":     ["INFY.NS", "WIPRO.NS", "TCS.NS", "HCLTECH.NS",
                     "TECHM.NS", "MPHASIS.NS"],
    "NIFTY_PHARMA": ["SUNPHARMA.NS", "CIPLA.NS", "DRREDDY.NS", "LUPIN.NS",
                     "AUROPHARMA.NS", "DIVISLAB.NS"],
    "NIFTY_AUTO":   ["TATAMOTORS.NS", "M&M.NS", "MARUTI.NS", "HEROMOTOCO.NS",
                     "BAJAJ-AUTO.NS", "EICHERMOT.NS", "ASHOKLEY.NS"],
}
VALIDATION_THRESHOLD = 0.90
TRAIL = 63          # ~one quarter of sessions for the liquidity weight
MIN_TRAIL = 20


def _fetch(tickers, yf, pd):
    """{ticker: (adj_close Series, volume Series)} + first-date coverage."""
    closes, volumes, coverage = {}, {}, {}
    for t in tickers:
        try:
            df = yf.download(t, start="1995-01-01", progress=False,
                             auto_adjust=True)
        except Exception as exc:
            print(f"    ! {t}: {type(exc).__name__}"[:60], file=sys.stderr)
            continue
        if df is None or len(df) == 0:
            print(f"    ! {t}: no data", file=sys.stderr)
            continue
        c, v = df["Close"], df["Volume"]
        if hasattr(c, "columns"):
            c, v = c.iloc[:, 0], v.iloc[:, 0]
        c.index, v.index = c.index.normalize(), v.index.normalize()
        closes[t], volumes[t] = c, v
        coverage[t] = c.index.min().date().isoformat()
    return closes, volumes, coverage


def _proxy_schemes(tickers, yf, pd):
    """Two LOOK-AHEAD-FREE proxy daily-return series: expanded equal-weight and
    trailing-liquidity-weight. Returns ({scheme: returns}, coverage)."""
    closes, volumes, coverage = _fetch(tickers, yf, pd)
    if not closes:
        return None, {}
    px = pd.DataFrame(closes).sort_index()
    vol = pd.DataFrame(volumes).reindex(px.index)
    rets = px.pct_change()

    equal = rets.mean(axis=1)                       # diversification-only

    turnover = px * vol                             # split-consistent traded value
    w = turnover.rolling(TRAIL, min_periods=MIN_TRAIL).mean().shift(1)
    w = w.where(rets.notna())                       # only weight names live that day
    liquidity = (w * rets).sum(axis=1) / w.sum(axis=1)

    return {"equal_weight": equal, "liquidity_weight": liquidity}, coverage


def _official_returns(key, pd):
    rows = [(pd.Timestamp(d), v) for d, v in MF.read_series(key) if v is not None]
    if not rows:
        return None
    s = pd.Series(dict(rows)).sort_index()
    s.index = s.index.normalize()
    return s.pct_change()


def _score(proxy, official, pd):
    j = pd.concat([proxy.rename("p"), official.rename("o")],
                  axis=1, join="inner").dropna()
    if len(j) < 250:
        return None
    corr = float(j["p"].corr(j["o"]))
    te = float((j["p"] - j["o"]).std() * (252 ** 0.5))
    return corr, te, len(j), j.index.min().date(), j.index.max().date()


def validate(threshold):
    try:
        import pandas as pd
        import yfinance as yf
    except ImportError as exc:
        sys.exit(f"needs pandas + yfinance: {exc}")

    print(f"Proxy Validation Protocol v2 — PREFER threshold r>={threshold}\n")
    summary = []
    for key, tickers in CONSTITUENTS.items():
        print(f"[{key}] {len(tickers)} names: {', '.join(tickers)}")
        schemes, cov = _proxy_schemes(tickers, yf, pd)
        official = _official_returns(key, pd)
        print(f"    coverage: " + ", ".join(f"{t.split('.')[0]}:{d}"
                                             for t, d in cov.items()))
        if not schemes or official is None:
            print("    -> could not build\n")
            summary.append((key, {}))
            continue
        scored = {}
        for name, proxy in schemes.items():
            res = _score(proxy, official, pd)
            if res is None:
                print(f"    {name:16s}: overlap too thin")
                continue
            corr, te, n, lo, hi = res
            ok = corr >= threshold
            scored[name] = corr
            print(f"    {name:16s}: r={corr:.4f}  TE={te:.1%}/yr  "
                  f"overlap {lo}..{hi} ({n}d)  "
                  f"{'VALIDATED' if ok else 'below bar'}")
        summary.append((key, scored))
        print()

    print("=" * 66)
    print(f"SUMMARY — best honest construction per sector (bar r>={threshold})")
    for key, scored in summary:
        if not scored:
            print(f"  {key:14s} — could not validate")
            continue
        best_scheme = max(scored, key=scored.get)
        best = scored[best_scheme]
        mark = "✓ VALIDATED -> PREFER-eligible" if best >= threshold \
            else "✗ SHOW-only"
        print(f"  {key:14s} best r={best:.3f} ({best_scheme})  {mark}")
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=VALIDATION_THRESHOLD)
    args = ap.parse_args()
    validate(args.threshold)


if __name__ == "__main__":
    main()
