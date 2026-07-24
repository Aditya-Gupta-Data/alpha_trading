#!/usr/bin/env python3
"""
scripts/build_synthetic_proxies.py — Proxy Validation Protocol (owner directive 2026-07-24)
===========================================================================================

Deep-past episodes (1997/2000/2001) predate even the "deep" official sector
indices (NIFTY IT starts 2002, Pharma 2005, Auto 2004), so no sector leg can
be priced there. The workaround: SYNTHETIC PROXY indices built from the
legacy stalwarts that defined each sector — an equal-weighted average of their
adjusted closes, back to 1995.

But a proxy built from TODAY's survivors risks survivorship bias, so it must
EARN the right to be trusted. This script runs the owner's validation:

  1. Build each proxy over the FULL horizon (1995 -> today), point-in-time
     (a constituent contributes only from its own listing date — no
     anachronistic membership).
  2. Score it OUT-OF-SAMPLE against the OFFICIAL NIFTY sector index over their
     overlapping years: Pearson correlation of DAILY RETURNS + annualized
     tracking error.
  3. DYNAMIC Guardrail #3: correlation >= THRESHOLD (0.90) => the proxy is
     mathematically validated and its deep-past legs may contribute to a
     PREFER verdict; below => the proxy stays SHOW-only.

Adjusted closes for the constituents (split/dividend-clean returns); the
official index is a level series (no splits). Requires yfinance + pandas
(Mac-lane utility, not a VM dep).

CLI: python3 scripts/build_synthetic_proxies.py [--threshold 0.90]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analysis import macro_features as MF

# sector lake KEY -> the legacy stalwarts (owner-provided), Yahoo .NS tickers.
# Point-in-time is automatic: yfinance returns each name only from its listing,
# so TCS (2004) simply doesn't enter the IT proxy before it existed.
CONSTITUENTS = {
    "NIFTY_IT":     ["INFY.NS", "WIPRO.NS", "TCS.NS"],
    "NIFTY_PHARMA": ["SUNPHARMA.NS", "CIPLA.NS", "DRREDDY.NS"],
    "NIFTY_AUTO":   ["TATAMOTORS.NS", "M&M.NS", "MARUTI.NS"],
}
VALIDATION_THRESHOLD = 0.90


def _proxy_daily_returns(tickers, yf, pd):
    """Equal-weighted daily returns of the available constituents (point-in-
    time). Returns (returns_series, {ticker: first_date})."""
    closes, coverage = {}, {}
    for t in tickers:
        try:
            df = yf.download(t, start="1995-01-01", progress=False,
                             auto_adjust=True)
        except Exception as exc:
            print(f"    ! {t}: {type(exc).__name__}: {exc}"[:80], file=sys.stderr)
            continue
        if df is None or len(df) == 0:
            print(f"    ! {t}: no data", file=sys.stderr)
            continue
        s = df["Close"]
        if hasattr(s, "columns"):                       # flatten MultiIndex
            s = s.iloc[:, 0]
        s.index = s.index.normalize()
        closes[t] = s
        coverage[t] = s.index.min().date().isoformat()
    if not closes:
        return None, {}
    px = pd.DataFrame(closes).sort_index()
    proxy = px.pct_change().mean(axis=1)                # equal-weight available
    return proxy, coverage


def _official_daily_returns(key, pd):
    rows = [(pd.Timestamp(d), v) for d, v in MF.read_series(key) if v is not None]
    if not rows:
        return None
    s = pd.Series(dict(rows)).sort_index()
    s.index = s.index.normalize()
    return s.pct_change()


def validate(threshold):
    try:
        import pandas as pd
        import yfinance as yf
    except ImportError as exc:
        sys.exit(f"needs pandas + yfinance: {exc}")

    print(f"Proxy Validation Protocol — corr threshold {threshold} for PREFER "
          f"eligibility\n")
    results = []
    for key, tickers in CONSTITUENTS.items():
        print(f"[{key}] constituents: {', '.join(tickers)}")
        proxy, cov = _proxy_daily_returns(tickers, yf, pd)
        official = _official_daily_returns(key, pd)
        if proxy is None or official is None:
            print(f"    -> could not build (proxy={proxy is not None}, "
                  f"official={official is not None})\n")
            results.append((key, None, None, None))
            continue
        joined = pd.concat([proxy.rename("proxy"), official.rename("off")],
                           axis=1, join="inner").dropna()
        if len(joined) < 250:
            print(f"    -> overlap too thin ({len(joined)} days)\n")
            results.append((key, None, None, len(joined)))
            continue
        corr = float(joined["proxy"].corr(joined["off"]))
        te = float((joined["proxy"] - joined["off"]).std() * (252 ** 0.5))
        lo, hi = joined.index.min().date(), joined.index.max().date()
        verdict = "VALIDATED -> PREFER-eligible" if corr >= threshold \
            else "FAILED -> stays SHOW-only"
        for t, first in cov.items():
            print(f"      {t:16s} from {first}")
        print(f"    overlap {lo}..{hi}  ({len(joined)} days)")
        print(f"    Pearson r = {corr:.4f} | tracking error = {te:.1%}/yr "
              f"=> {verdict}\n")
        results.append((key, corr, te, len(joined)))

    print("=" * 64)
    print("SUMMARY — did the proxies earn the right to trigger PREFER?")
    for key, corr, te, n in results:
        if corr is None:
            print(f"  {key:14s} — could not validate")
        else:
            mark = "✓ VALIDATED" if corr >= threshold else "✗ SHOW-only"
            print(f"  {key:14s} r={corr:.3f}  TE={te:.1%}/yr  {mark}")
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=VALIDATION_THRESHOLD)
    args = ap.parse_args()
    validate(args.threshold)


if __name__ == "__main__":
    main()
