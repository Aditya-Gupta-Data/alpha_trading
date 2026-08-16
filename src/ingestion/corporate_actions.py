"""
src/ingestion/corporate_actions.py — split / bonus adjustment for HISTORICAL bars
================================================================================

WHY (2026-08-16, Sequence 2 / Gap 3). `data/lake/bhavcopy/` stores RAW
exchange closes, and NSE's own PREV_CLOSE column is NOT adjusted on an
ex-date — measured in the lake itself:

    IRCTC     2021-10-28  prev_close 4130.15  open   817.0   (1:5  split)
    TATASTEEL 2022-07-28  prev_close  959.4   open    98.1   (1:10 split)
    NESTLEIND 2024-01-05  prev_close 27116.4  open  2754.0   (1:10 split)

Every analytical lookback that reads bars (`analysis/darling_tiers`,
`analysis/dynamic_pricer`, `analysis/patience_basket`,
`analysis/underlying_router`, `analysis/valuation_scorer`,
`equity_shadow_proposer`) would read those as −80% / −90% crashes: SMAs
break, entry zones fall to nonsense, "cheapness" scores explode.

THE CONTRACT — read this before wiring anything
-----------------------------------------------
* Adjustment is BACKWARD: every bar strictly BEFORE an ex_date is divided
  by the cumulative ratio of all actions on/after it (prices ÷ ratio,
  volume × ratio). **The bar on the ex_date and every later bar are never
  touched.** So the LATEST bar — the one live marks, EOD quotes and any
  execution logic read — is always the raw exchange print. Historical
  analytics get a continuous series; execution stays raw by construction.
* Adjusted bars carry `adj_factor` (>1 means "scaled") and `raw_close`,
  so nothing is hidden: a consumer can always see what the exchange
  printed.
* Two sources of actions, in this order of authority:
    1. `config/corporate_actions.json` — the CONFIRMED dictionary (manual,
       schema in the file). Authoritative.
    2. `detect_candidates(bars)` — the lake's OWN evidence: a one-session
       prev_close→open gap that snaps to a canonical split/bonus ratio
       (2, 3, 4, 5, 10, 20, 1.5) within `SNAP_TOL`, with a
       volume surge ≥ `VOLUME_SURGE_MIN`. Used automatically ONLY when
       the caller asks (`auto=True`); by default it is a review tool
       (`--scan`) whose output the owner confirms into the JSON.
  RULE 3 applies: a ratio is never invented. No config row + no lake
  evidence ⇒ the cliff stays, visible, rather than a guessed factor.
* Optional yfinance cross-check (`--yf-check`, MAC-ONLY, function-local
  import): yfinance closes are split-adjusted, so raw_ratio / yf_ratio
  across the ex-date IS the multiplier. Never run on the VM.

WHAT THIS DOES NOT DO. Dividends (small base-price steps) are ignored on
purpose — a 1-2% ex-dividend drop is real cash leaving the company, not a
unit change, and adjusting it would fabricate total-return series the
system never claimed to have. Rights issues are out of scope (their
"ratio" depends on the issue price and needs a circular).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "corporate_actions.json"

PRICE_FIELDS = ("open", "high", "low", "close", "prev_close", "avg_price")
CANONICAL_RATIOS = (10.0, 5.0, 4.0, 3.0, 2.0, 20.0, 1.5)   # 1.25/1.2 dropped: indistinguishable from an OFS/news gap (IRCTC 2020-12-10 measured 1.15, false positive)
SNAP_TOL = 0.06            # |measured/canonical − 1| ≤ 6% (ex-day drift)
VOLUME_SURGE_MIN = 2.0     # ex-day volume ÷ prior-day volume
GAP_MIN = 1.30             # below this a prev_close→open gap is just a gap


# ---------------------------------------------------------------- config

def load_actions(path=None) -> list:
    """Confirmed actions from the JSON, [] if absent/unreadable.
    Rows missing symbol/ex_date/ratio, or with ratio <= 0, are dropped —
    a half-row must never become a factor."""
    try:
        raw = json.loads(Path(path or CONFIG_PATH).read_text())
    except (OSError, ValueError):
        return []
    out = []
    for r in raw.get("actions") or []:
        try:
            ratio = float(r.get("ratio"))
        except (TypeError, ValueError):
            continue
        if not r.get("symbol") or not r.get("ex_date") or ratio <= 0:
            continue
        out.append({"symbol": str(r["symbol"]).upper(),
                    "ex_date": str(r["ex_date"]), "ratio": ratio,
                    "kind": r.get("kind") or "other",
                    "source": r.get("source") or "config",
                    "verified_against_nse_circular":
                        bool(r.get("verified_against_nse_circular", False))})
    return out


def actions_for(symbol: str, actions: list = None, path=None) -> list:
    sym = str(symbol or "").split(".")[0].upper()
    acts = load_actions(path) if actions is None else actions
    return sorted((a for a in acts if a["symbol"] == sym),
                  key=lambda a: a["ex_date"])


# ------------------------------------------------------------- detection

def _snap(measured: float):
    best, err = None, None
    for c in CANONICAL_RATIOS:
        e = abs(measured / c - 1)
        if err is None or e < err:
            best, err = c, e
    return (best, err) if err is not None and err <= SNAP_TOL else (None, err)


def detect_candidates(bars: list, symbol: str = None) -> list:
    """Candidate splits/bonuses from the lake's own discontinuities:
    prev_close(ex-day) ÷ open(ex-day) ≥ GAP_MIN, snapping to a canonical
    ratio, with a volume surge. Returns [{symbol, ex_date, ratio,
    measured_ratio, volume_surge, kind: 'candidate', source}] — REVIEW
    output, not truth: a genuine −80% crash with 5x volume on one day
    would also land here, which is exactly why the config wins."""
    out, prev = [], None
    for b in bars or []:
        try:
            pc, op = float(b.get("prev_close") or 0), float(b.get("open") or 0)
        except (TypeError, ValueError):
            prev = b; continue
        if pc > 0 and op > 0 and pc / op >= GAP_MIN:
            snapped, err = _snap(pc / op)
            v_now = float(b.get("volume") or 0)
            v_prev = float((prev or {}).get("volume") or 0)
            surge = (v_now / v_prev) if v_prev > 0 else None
            if snapped and (surge is None or surge >= VOLUME_SURGE_MIN):
                out.append({"symbol": symbol, "ex_date": b.get("session") or b.get("date"),
                            "ratio": snapped, "measured_ratio": round(pc / op, 4),
                            "volume_surge": round(surge, 2) if surge else None,
                            "kind": "candidate",
                            "source": f"bhavcopy_gap: prev_close {pc} -> open {op}",
                            "verified_against_nse_circular": False})
        prev = b
    return out


# ------------------------------------------------------------ adjustment

def adjust_bars(bars: list, actions: list) -> list:
    """New list of bars with every bar strictly BEFORE each ex_date scaled
    by the cumulative ratio of actions on/after it. Bars on/after the
    latest ex_date are returned unchanged (adj_factor 1.0). Input is not
    mutated. No actions ⇒ the same bars, adj_factor 1.0 stamped."""
    acts = sorted((a for a in actions or [] if a.get("ratio")),
                  key=lambda a: a["ex_date"])
    out = []
    for b in bars or []:
        session = str(b.get("session") or b.get("date") or "")
        factor = 1.0
        for a in acts:
            if session and session < a["ex_date"]:
                factor *= float(a["ratio"])
        nb = dict(b)
        nb["raw_close"] = b.get("close")
        nb["adj_factor"] = round(factor, 6)
        if factor != 1.0:
            for f in PRICE_FIELDS:
                v = b.get(f)
                if v is not None:
                    try:
                        nb[f] = round(float(v) / factor, 4)
                    except (TypeError, ValueError):
                        pass
            for f in ("volume", "deliv_qty"):
                v = b.get(f)
                if v is not None:
                    try:
                        nb[f] = round(float(v) * factor, 0)
                    except (TypeError, ValueError):
                        pass
        out.append(nb)
    return out


def adjusted(symbol: str, bars: list, actions: list = None, path=None,
             auto: bool = False) -> list:
    """The one-call door: config actions (+ lake-detected candidates when
    `auto=True`, de-duplicated on ex_date, config wins) applied to `bars`."""
    acts = actions_for(symbol, actions, path)
    if auto:
        have = {a["ex_date"] for a in acts}
        acts += [c for c in detect_candidates(bars, symbol)
                 if c["ex_date"] not in have]
    return adjust_bars(bars, acts)


# ------------------------------------------------------- yfinance check

def yfinance_ratio(symbol: str, ex_date: str, raw_bars: list,
                   fetch_fn=None) -> dict:
    """MAC-ONLY cross-check. yfinance closes are split-adjusted, so
    raw_ratio ÷ yf_ratio across the ex-date is the multiplier. `fetch_fn`
    (tests) returns {date: close}; the default imports yfinance
    function-locally (never at module import — src/ must stay VM-safe)."""
    prev = nxt = None
    for b in raw_bars or []:
        s = str(b.get("session") or b.get("date") or "")
        if s < ex_date:
            prev = b
        elif s >= ex_date and nxt is None:
            nxt = b
    if not prev or not nxt or not prev.get("close") or not nxt.get("close"):
        return {"ok": False, "reason": "raw bars do not bracket the ex_date"}
    if fetch_fn is None:
        def fetch_fn(sym):
            import yfinance as yf                      # Mac-lane dependency
            h = yf.Ticker(f"{sym}.NS").history(period="max", auto_adjust=True)
            return {d.strftime("%Y-%m-%d"): float(c) for d, c in h["Close"].items()}
    try:
        yfc = fetch_fn(symbol)
    except Exception as e:
        return {"ok": False, "reason": f"yfinance unavailable: {e}"}
    p_s = str(prev.get("session") or prev.get("date"))
    n_s = str(nxt.get("session") or nxt.get("date"))
    if p_s not in yfc or n_s not in yfc or not yfc[n_s]:
        return {"ok": False, "reason": "yfinance lacks one of the two sessions"}
    raw_ratio = float(prev["close"]) / float(nxt["close"])
    yf_ratio = yfc[p_s] / yfc[n_s]
    mult = raw_ratio / yf_ratio
    snapped, err = _snap(mult)
    return {"ok": True, "raw_ratio": round(raw_ratio, 4),
            "yf_ratio": round(yf_ratio, 4), "multiplier": round(mult, 4),
            "snapped_ratio": snapped, "snap_error": round(err, 4) if err is not None else None}


# ------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="corporate-action adjuster tools")
    ap.add_argument("--scan", metavar="SYMBOL", help="list candidate ex-dates from the lake")
    ap.add_argument("--yf-check", nargs=2, metavar=("SYMBOL", "EX_DATE"),
                    help="MAC-ONLY yfinance multiplier cross-check")
    ap.add_argument("--days", type=int, default=3000)
    a = ap.parse_args(argv)
    from src.ingestion.bhavcopy_clerk import bars_for
    if a.scan:
        raw = bars_for(a.scan, days=a.days, adjust=False)
        conf = {x["ex_date"] for x in actions_for(a.scan)}
        cands = detect_candidates(raw, a.scan)
        print(f"{a.scan}: {len(raw)} raw bars, {len(cands)} candidate(s), "
              f"{len(conf)} already confirmed in config")
        for c in cands:
            tag = "CONFIRMED" if c["ex_date"] in conf else "candidate"
            print(f"  {c['ex_date']}  ratio~{c['ratio']:g} (measured {c['measured_ratio']}, "
                  f"vol x{c['volume_surge']})  [{tag}]")
        return 0
    if a.yf_check:
        sym, ex = a.yf_check
        raw = bars_for(sym, days=a.days, adjust=False)
        print(json.dumps(yfinance_ratio(sym, ex, raw), indent=1))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
