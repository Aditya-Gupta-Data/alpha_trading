"""
src/liquidity_slippage.py — the liquidity-tier slippage ladder (Gap 4)
======================================================================

WHY (2026-08-17, Sequence 3). The paper engine already models bid-ask
slippage for OPTIONS (premium ladder 0.10–0.50% × VIX blowout × book
depth, `plan_tracker.apply_slippage`) and INDEX (0.05%) — but STOCK
slippage was a flat **0.0%**, and nothing anywhere asked how liquid the
name actually is. A frictionless equity fill on an illiquid name is a
fantasy fill, and every downstream learner (adaptive sizing, the miners'
`outcomes`) would learn from it.

THE LADDER — one door, read from `data/fo_liquidity.json` (the same file
the option halt-stack and the insolvency gate read; one liquidity truth):

    tier1 (top-25 by stock-option value, or an INDEX)   0.10 %
    tier2 (in F&O, not tier1)                           0.25 %
    illiquid (not in F&O / banned / file missing)       0.50 %

Per side, on the fill price. `symbol=None` (every legacy caller) keeps
the old numbers byte-for-byte — the tier only bites when a caller names
the instrument. PAPER ONLY: this adjusts simulated fills and settled
P&L; there is no broker path in `src/` and this module sends nothing
anywhere.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FO_PATH = ROOT / "data" / "fo_liquidity.json"

TIER_SLIPPAGE = {"tier1": 0.0010, "tier2": 0.0025, "illiquid": 0.0050}
INDEX_MARKERS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "MIDCPNIFTY")

_cache = {"path": None, "mtime": None, "data": None}


def _load(path=None) -> dict | None:
    p = Path(path or FO_PATH)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None
    if _cache["path"] == str(p) and _cache["mtime"] == mtime:
        return _cache["data"]
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    _cache.update(path=str(p), mtime=mtime, data=data)
    return data


def liquidity_tier(symbol: str, path=None) -> str:
    """'tier1' | 'tier2' | 'illiquid'. Indices are tier1 by definition;
    a name in the F&O ban list, absent from the file, or with no file at
    all is 'illiquid' — the honest default is the EXPENSIVE one."""
    sym = str(symbol or "").split(".")[0].strip().upper().replace(" ", "")
    if not sym:
        return "illiquid"
    if any(m in sym for m in INDEX_MARKERS):
        return "tier1"
    fo = _load(path)
    if not fo:
        return "illiquid"
    if sym in {str(b).upper() for b in (fo.get("banned") or [])}:
        return "illiquid"
    row = (fo.get("symbols") or {}).get(sym)
    if not row:
        return "illiquid"
    return "tier1" if row.get("tier") == "tier1" else "tier2"


def tier_slippage_frac(symbol: str, path=None) -> float:
    return TIER_SLIPPAGE[liquidity_tier(symbol, path)]


def slippage_rs(price: float, qty: int, symbol: str, path=None) -> float:
    """Rupees of slippage for ONE side of a fill of `qty` at `price`."""
    try:
        return round(float(price) * int(qty) * tier_slippage_frac(symbol, path), 2)
    except (TypeError, ValueError):
        return 0.0
