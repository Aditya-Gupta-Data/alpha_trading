"""
src/analysis/gamma_profile.py — per-strike Gamma Exposure (GEX) from a Dhan option chain (decision #152).

Pure: no I/O, no market data door, no trade path. Input is the chain's own `oc` dict (the shape
`dhan_client.get_option_chain` returns and `chain_archiver` stores); output is per-strike rows and one
profile. Advisory only — nothing reads it to trade until a Department 5 ruling on shadow evidence.

Units: GEX(K) = gamma(K) x OI(K) x spot^2 x 0.01 = rupees of delta per 1% move. OI is taken as reported
(Dhan reports quantity, not contracts — `oi_lot_multiple` measures that per chain).
Sign (D2, NAIVE, unverified for NSE): dealers long calls, short puts -> calls +, puts -.
The flip is the strike-ordered cumulative-net zero crossing — an approximation (gamma is read at today's
spot only, never re-priced at hypothetical spots).
"""
from __future__ import annotations

PCT = 0.01
DEFAULT_BAND_PCT = {"index": 5.0, "stock": 10.0}     # D3: strikes within this % of spot
MIN_COVERAGE = 0.8                                   # share of in-band strikes with gamma+OI on both sides


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _side(node: dict, sign: float, spot: float) -> dict:
    """One ce/pe node -> {gamma, oi, doi, gex, doi_gex}; gex None when gamma or OI is missing."""
    node = node if isinstance(node, dict) else {}
    gamma = _num((node.get("greeks") or {}).get("gamma"))
    oi = _num(node.get("oi"))
    prev = _num(node.get("previous_oi"))
    doi = oi - prev if oi is not None and prev is not None else None
    scale = sign * spot * spot * PCT
    return {"gamma": gamma, "oi": oi, "doi": doi,
            "gex": gamma * oi * scale if gamma is not None and oi is not None else None,
            "doi_gex": gamma * doi * scale if gamma is not None and doi is not None else None}


def strike_gex(oc: dict, spot) -> list:
    """Per-strike GEX rows sorted by strike: call/put/net/abs GEX, the OI-change variant, completeness."""
    spot = _num(spot)
    if not spot or spot <= 0 or not isinstance(oc, dict):
        return []
    rows = []
    for key, node in oc.items():
        strike = _num(key)
        if strike is None or not isinstance(node, dict):
            continue
        c, p = _side(node.get("ce"), 1.0, spot), _side(node.get("pe"), -1.0, spot)
        complete = c["gex"] is not None and p["gex"] is not None
        rows.append({
            "strike": strike,
            "call_gex": c["gex"], "put_gex": p["gex"],
            "net_gex": (c["gex"] or 0.0) + (p["gex"] or 0.0) if complete else None,
            "abs_gex": abs(c["gex"] or 0.0) + abs(p["gex"] or 0.0) if complete else None,
            "call_doi_gex": c["doi_gex"], "put_doi_gex": p["doi_gex"],
            "call_oi": c["oi"], "put_oi": p["oi"], "call_gamma": c["gamma"], "put_gamma": p["gamma"],
            "complete": complete})
    return sorted(rows, key=lambda r: r["strike"])


def merge_rows(*row_sets) -> list:
    """Strike rows of several expiries summed per strike (a strike is complete only if every set has it complete)."""
    out = {}
    for rows in row_sets:
        for r in rows or []:
            m = out.setdefault(r["strike"], {"strike": r["strike"], "call_gex": 0.0, "put_gex": 0.0,
                                             "call_doi_gex": 0.0, "put_doi_gex": 0.0, "complete": True})
            for k in ("call_gex", "put_gex", "call_doi_gex", "put_doi_gex"):
                m[k] = None if m[k] is None or r.get(k) is None else m[k] + r[k]
            m["complete"] = m["complete"] and bool(r.get("complete"))
    for m in out.values():
        ok = m["complete"] and m["call_gex"] is not None and m["put_gex"] is not None
        m["net_gex"] = m["call_gex"] + m["put_gex"] if ok else None
        m["abs_gex"] = abs(m["call_gex"]) + abs(m["put_gex"]) if ok else None
        m["complete"] = ok
    return sorted(out.values(), key=lambda r: r["strike"])


def _flip(rows: list):
    """The level where the strike-ordered cumulative net GEX crosses zero (linear between strikes), or None."""
    cum, prev = 0.0, None
    for r in rows:
        before = cum
        cum += r["net_gex"]
        if prev is not None and before != 0 and (before < 0) != (cum < 0):
            k0, k1 = prev, r["strike"]
            return round(k0 + (k1 - k0) * (abs(before) / (abs(before) + abs(cum))), 2)
        prev = r["strike"]
    return None


def oi_lot_multiple(rows: list, lot_size) -> float | None:
    """Share of nonzero OI values that are whole multiples of `lot_size` (~1.0 = OI is quantity, not contracts)."""
    lot = _num(lot_size)
    vals = [v for r in rows for v in (r.get("call_oi"), r.get("put_oi")) if v]
    if not lot or lot <= 0 or not vals:
        return None
    return round(sum(1 for v in vals if abs(v / lot - round(v / lot)) < 1e-9) / len(vals), 3)


def profile(rows: list, spot, band_pct: float) -> dict:
    """One profile over the strikes within `band_pct` of spot: regime, walls, flip, totals, concentration.
    Abstains (regime None + reason) when fewer than MIN_COVERAGE of in-band strikes are complete."""
    spot = _num(spot)
    out = {"spot": spot, "band_pct": band_pct, "strikes_in_band": 0, "coverage": None,
           "regime": None, "net_gex": None, "abs_gex": None, "net_share": None,
           "net_doi_gex": None, "call_wall": None, "put_wall": None, "flip": None,
           "spot_vs_flip_pct": None, "concentration": None, "reason": None}
    if not spot or spot <= 0:
        out["reason"] = "no spot"
        return out
    lo, hi = spot * (1 - band_pct / 100.0), spot * (1 + band_pct / 100.0)
    band = [r for r in rows or [] if lo <= r["strike"] <= hi]
    out["strikes_in_band"] = len(band)
    if not band:
        out["reason"] = f"no strikes within {band_pct:g}% of spot"
        return out
    full = [r for r in band if r.get("complete")]
    out["coverage"] = round(len(full) / len(band), 3)
    if out["coverage"] < MIN_COVERAGE:
        out["reason"] = (f"only {len(full)}/{len(band)} in-band strikes carry gamma and OI on both sides "
                         f"(< {MIN_COVERAGE:.0%})")
        return out
    net = sum(r["net_gex"] for r in full)
    gross = sum(r["abs_gex"] for r in full)
    doi = [r["call_doi_gex"] + r["put_doi_gex"] for r in full
           if r.get("call_doi_gex") is not None and r.get("put_doi_gex") is not None]
    call_wall = max(full, key=lambda r: r["call_gex"])
    put_wall = min(full, key=lambda r: r["put_gex"])
    flip = _flip(full)
    out.update(
        regime="positive" if net > 0 else "negative",
        net_gex=round(net, 2), abs_gex=round(gross, 2),
        net_share=round(net / gross, 4) if gross else None,
        net_doi_gex=round(sum(doi), 2) if doi else None,
        call_wall=call_wall["strike"], put_wall=put_wall["strike"], flip=flip,
        spot_vs_flip_pct=round((spot / flip - 1) * 100, 3) if flip else None,
        concentration=round(max(r["abs_gex"] for r in full) / gross, 4) if gross else None)
    return out
