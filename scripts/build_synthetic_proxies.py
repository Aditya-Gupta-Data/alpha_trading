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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.analysis import macro_features as MF

MANIFEST_PATH = ROOT / "data" / "macro_proxy_manifest.json"
# INJECTION DISABLED (owner directive 2026-07-24). Pharma CLEARED the 0.90 bar
# on the 2005+ overlap — but the deep-past USAGE region (pre-2005) has NO
# official index to validate against AND the raw Yahoo data is glitch-ridden
# (CIPLA carried a +1224% single-day print that inflated the cumulative proxy
# ~3000x). A proxy validated where we don't need it and untrustworthy where we
# do is not injectable. `inject()` stays as working tooling for the day a CLEAN
# deep-past sector source exists; until then VALIDATED is empty and the
# deep-past episodes carry NULL-honest sector legs — they strengthen the
# CLUSTERING, not sector-strategy analog counts.
VALIDATED = {}

# sector lake KEY -> 5-8 legacy stalwarts (owner-provided core + expansion),
# Yahoo .NS tickers. Point-in-time membership is automatic (late listers
# simply contribute nothing before their listing).
CONSTITUENTS = {
    "NIFTY_IT":     ["INFY.NS", "WIPRO.NS", "TCS.NS", "HCLTECH.NS",
                     "TECHM.NS", "MPHASIS.NS"],
    "NIFTY_PHARMA": ["SUNPHARMA.NS", "CIPLA.NS", "DRREDDY.NS", "LUPIN.NS",
                     "AUROPHARMA.NS", "DIVISLAB.NS"],
    # TATAMOTORS is UNAVAILABLE on Yahoo (all of .NS/.BO/DVR/old-TELCO 404 —
    # the 2024-25 CV/PV demerger orphaned the historical series). Best-effort
    # fallbacks exhausted; compensated with Bosch (ancillary) + TVS (2-wheeler),
    # both genuine deep-history stalwarts.
    "NIFTY_AUTO":   ["M&M.NS", "MARUTI.NS", "HEROMOTOCO.NS", "BAJAJ-AUTO.NS",
                     "EICHERMOT.NS", "ASHOKLEY.NS", "BOSCHLTD.NS", "TVSMOTOR.NS"],
}
VALIDATION_THRESHOLD = 0.90
TRAIL = 63          # ~one quarter of sessions for the liquidity weight
MIN_TRAIL = 20

# LOCAL overrides: owner-supplied CSVs (date,close,volume) for heavyweights
# Yahoo can't serve — spliced into the basket exactly like a fetched name.
# TATAMOTORS is 404 on Yahoo (demerger-orphaned); the owner supplied real
# 2000-2019 data (investing.com), consolidated into drop/TATAMOTORS-LOCAL.csv.
LOCAL_OVERRIDES = {
    "NIFTY_AUTO": {"TATAMOTORS": Path(__file__).resolve().parents[1]
                   / "drop" / "TATAMOTORS-LOCAL.csv"},
}


def _load_local(name, path, pd):
    """Owner-supplied local CSV (date,close,volume) -> (close, volume) Series,
    normalized. Splits/dividends assumed already adjusted (verified at
    consolidation). Returns (None, None) if the file is absent."""
    p = Path(path)
    if not p.exists():
        print(f"    ! {name}: local file not found ({p.name})", file=sys.stderr)
        return None, None
    df = pd.read_csv(p, parse_dates=["date"]).set_index("date").sort_index()
    df.index = df.index.normalize()
    vol = df["volume"] if "volume" in df.columns else None
    return df["close"], vol


def _fetch(tickers, overrides, yf, pd):
    """{name: (adj_close, volume)} from Yahoo + any local overrides."""
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
    for name, path in (overrides or {}).items():
        c, v = _load_local(name, path, pd)
        if c is not None:
            closes[name], volumes[name] = c, v
            coverage[name] = c.index.min().date().isoformat() + " [local]"
    return closes, volumes, coverage


def _proxy_schemes(tickers, overrides, yf, pd):
    """Two LOOK-AHEAD-FREE proxy daily-return series: expanded equal-weight and
    trailing-liquidity-weight. Returns ({scheme: returns}, coverage)."""
    closes, volumes, coverage = _fetch(tickers, overrides, yf, pd)
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
        overrides = LOCAL_OVERRIDES.get(key, {})
        allnames = tickers + [f"{n} (local)" for n in overrides]
        print(f"[{key}] {len(allnames)} names: {', '.join(allnames)}")
        schemes, cov = _proxy_schemes(tickers, overrides, yf, pd)
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


def inject(dry_run=False):
    """Splice each VALIDATED proxy into its lake key BELOW the official floor,
    scaled to meet the official index at the splice (continuity). Merge keeps
    the official values authoritative (stored wins); only pre-official dates
    are filled. Provenance is stamped in data/macro_proxy_manifest.json so the
    synthetic ranges are never mistaken for official (the source='proxy' tag —
    the lake CSV itself carries no source column)."""
    import json
    import pandas as pd
    import yfinance as yf
    from src.ingestion.index_history import merge_into_lake

    manifest = {"note": "pre-<proxy_before> lake values for these keys are "
                        "SYNTHETIC proxy (source='proxy'), NOT official.",
                "keys": {}}
    for key, scheme in VALIDATED.items():
        schemes, _ = _proxy_schemes(CONSTITUENTS[key],
                                    LOCAL_OVERRIDES.get(key, {}), yf, pd)
        pr = schemes[scheme].dropna()
        official = sorted((pd.Timestamp(d), v)
                          for d, v in MF.read_series(key) if v is not None)
        if not official or pr.empty:
            print(f"  {key}: cannot inject (proxy/official missing)")
            continue
        splice_ts, anchor = official[0]
        cum = (1 + pr).cumprod()
        base = cum.index[cum.index <= splice_ts]
        if len(base) == 0:
            print(f"  {key}: proxy does not reach the splice date")
            continue
        level = cum * (anchor / cum.loc[base[-1]])         # meet official at splice
        pre = level[level.index < splice_ts]
        rows = [(ts.date().isoformat(), round(float(v), 4)) for ts, v in pre.items()]
        summ = merge_into_lake(key, rows, dry_run=dry_run)
        manifest["keys"][key] = {
            "proxy_before": splice_ts.date().isoformat(), "scheme": scheme,
            "constituents": CONSTITUENTS[key]
            + [f"{n}(local)" for n in LOCAL_OVERRIDES.get(key, {})],
            "added": summ["added"], "floor": summ["floor"], "source": "proxy"}
        print(f"  {key}: +{summ['added']} proxy sessions before "
              f"{splice_ts.date()} (lake floor now {summ['floor']})"
              f"{' [dry-run]' if dry_run else ''}")
    if not dry_run:
        MANIFEST_PATH.write_text(json.dumps(manifest, indent=1, default=str))
        print(f"  manifest -> {MANIFEST_PATH.name}")
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=VALIDATION_THRESHOLD)
    ap.add_argument("--inject", action="store_true",
                    help="splice VALIDATED proxies into the lake below the "
                         "official floor (+ provenance manifest)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.inject:
        inject(dry_run=args.dry_run)
    else:
        validate(args.threshold)


if __name__ == "__main__":
    main()
