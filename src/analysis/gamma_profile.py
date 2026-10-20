"""
src/analysis/gamma_profile.py — per-strike Gamma Exposure (GEX) from a Dhan option chain (decision #152).

Pure: no I/O, no market data door, no trade path. Input is the chain's own `oc` dict (the shape
`dhan_client.get_option_chain` returns and `chain_archiver` stores); output is per-strike rows and one
profile. Advisory only — nothing reads it to trade until a Department 5 ruling on shadow evidence.

Units: GEX(K) = gamma(K) x OI(K) x spot^2 x 0.01 = rupees of delta per 1% move. OI is taken as reported
(Dhan reports quantity, not contracts — `oi_lot_multiple` measures that per chain).
Sign: NAIVE_WEIGHTS (dealers long calls, short puts) or weights calibrated from NSE participant-wise OI
(`ingestion.participant_oi.sign_weights`, seam 2): GEX_side = weight_side x gamma x OI x S^2 x 1%.
`flip` is the strike-ordered cumulative-net crossing (seam 1, an approximation); `repriced_profile` finds
the real zero-gamma level by re-pricing Black-Scholes gamma at shifted spots from each strike's OTM IV.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

PCT = 0.01
NAIVE_WEIGHTS = {"call": 1.0, "put": -1.0}
RISK_FREE = 0.065                                    # assumption; gamma barely moves with r
IV_BOUNDS = (1.0, 200.0)                             # per cent; outside = no usable IV
GRID_PCT = {"index": 6.0, "stock": 12.0}             # shifted spots for the zero-gamma search
GRID_STEPS = 49
_IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_BAND_PCT = {"index": 5.0, "stock": 10.0}     # D3: strikes within this % of spot
MIN_COVERAGE = 0.8                                   # share of in-band strikes with gamma+OI on both sides


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _side(node: dict, sign: float, spot: float) -> dict:
    """One ce/pe node -> {gamma, oi, doi, gex, doi_gex}; gex None when gamma or OI is missing.
    Dhan's zero-IV rows (gamma 0 on ITM strikes at EOD) are missing, never a real zero."""
    node = node if isinstance(node, dict) else {}
    gamma = _num((node.get("greeks") or {}).get("gamma"))
    if gamma == 0 and not _num(node.get("implied_volatility")):
        gamma = None
    oi = _num(node.get("oi"))
    prev = _num(node.get("previous_oi"))
    doi = oi - prev if oi is not None and prev is not None else None
    scale = sign * spot * spot * PCT
    return {"gamma": gamma, "oi": oi, "doi": doi,
            "gex": gamma * oi * scale if gamma is not None and oi is not None else None,
            "doi_gex": gamma * doi * scale if gamma is not None and doi is not None else None}


def strike_gex(oc: dict, spot, weights: dict = None) -> list:
    """Per-strike GEX rows sorted by strike from Dhan's own gamma: call/put/net/abs GEX, the OI-change
    variant, completeness. `weights` = {call, put} side signs/scales (default NAIVE_WEIGHTS)."""
    spot = _num(spot)
    w = weights or NAIVE_WEIGHTS
    if not spot or spot <= 0 or not isinstance(oc, dict):
        return []
    rows = []
    for key, node in oc.items():
        strike = _num(key)
        if strike is None or not isinstance(node, dict):
            continue
        c, p = _side(node.get("ce"), w["call"], spot), _side(node.get("pe"), w["put"], spot)
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
    call_wall = max(full, key=lambda r: abs(r["call_gex"]))          # largest exposure, whatever the sign
    put_wall = max(full, key=lambda r: abs(r["put_gex"]))
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


# ------------------------------------------------------------- seam 2: re-priced gamma (#153)

def years_to_expiry(expiry, as_of):
    """Calendar years from `as_of` (ISO; naive = IST) to expiry day 15:30 IST, or None when past/unreadable."""
    try:
        exp = datetime.fromisoformat(str(expiry)[:10]).replace(hour=15, minute=30, tzinfo=_IST)
        now = datetime.fromisoformat(str(as_of))
    except (TypeError, ValueError):
        return None
    now = now if now.tzinfo else now.replace(tzinfo=_IST)
    t = (exp - now).total_seconds() / (365.0 * 86400)
    return t if t > 0 else None


def bs_gamma(spot, strike, iv_pct, t_years, r: float = RISK_FREE):
    """Black-Scholes gamma per share (identical for call and put), or None on any unusable input."""
    S, K, iv, t = _num(spot), _num(strike), _num(iv_pct), _num(t_years)
    if not S or not K or S <= 0 or K <= 0 or not iv or not t or t <= 0:
        return None
    sigma = iv / 100.0
    root = sigma * math.sqrt(t)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * t) / root
    return math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) / (S * root)


def _iv(node: dict):
    v = _num((node or {}).get("implied_volatility"))
    return v if v is not None and IV_BOUNDS[0] <= v <= IV_BOUNDS[1] else None


def strike_inputs(oc: dict, spot) -> list:
    """Per strike: one IV (the OTM side's — puts below spot, calls above — else the other side's), its
    source, both OIs and Dhan's gammas. Dhan's EOD call/put IVs at one strike disagree; the OTM quote is
    the traded one."""
    spot = _num(spot)
    if not spot or not isinstance(oc, dict):
        return []
    out = []
    for key, node in oc.items():
        k = _num(key)
        if k is None or not isinstance(node, dict):
            continue
        ce, pe = node.get("ce") or {}, node.get("pe") or {}
        otm, itm, otm_name = (pe, ce, "pe") if k < spot else (ce, pe, "ce")
        iv, src = _iv(otm), otm_name
        if iv is None:
            iv, src = _iv(itm), ("ce" if otm_name == "pe" else "pe")
        out.append({"strike": k, "iv": iv, "iv_source": src if iv is not None else None,
                    "call_oi": _num(ce.get("oi")), "put_oi": _num(pe.get("oi")),
                    "dhan_call_gamma": _num((ce.get("greeks") or {}).get("gamma")),
                    "dhan_put_gamma": _num((pe.get("greeks") or {}).get("gamma"))})
    return sorted(out, key=lambda r: r["strike"])


def model_strike_gex(inputs: list, spot, t_years, weights: dict = None) -> list:
    """strike_gex-shaped rows with Black-Scholes gamma at `spot` from each strike's IV."""
    S, w = _num(spot), weights or NAIVE_WEIGHTS
    rows = []
    for r in inputs or []:
        g = bs_gamma(S, r["strike"], r["iv"], t_years)
        ok = g is not None and r["call_oi"] is not None and r["put_oi"] is not None
        scale = S * S * PCT if S else 0.0
        cg = w["call"] * g * r["call_oi"] * scale if ok else None
        pg = w["put"] * g * r["put_oi"] * scale if ok else None
        rows.append({"strike": r["strike"], "call_gex": cg, "put_gex": pg,
                     "net_gex": cg + pg if ok else None,
                     "abs_gex": abs(cg) + abs(pg) if ok else None,
                     "call_doi_gex": None, "put_doi_gex": None,
                     "call_oi": r["call_oi"], "put_oi": r["put_oi"], "complete": ok})
    return rows


def gamma_curve(inputs: list, spot, t_years, weights: dict = None, grid_pct: float = 6.0,
                steps: int = GRID_STEPS) -> list:
    """[(shifted spot, total net GEX)] across spot x (1 +/- grid_pct%), every complete strike re-priced."""
    S = _num(spot)
    if not S or not t_years or steps < 2:
        return []
    out = []
    for i in range(steps):
        s_i = S * (1 - grid_pct / 100.0 + 2 * grid_pct / 100.0 * i / (steps - 1))
        rows = [r for r in model_strike_gex(inputs, s_i, t_years, weights) if r["complete"]]
        out.append((round(s_i, 2), sum(r["net_gex"] for r in rows) if rows else None))
    return out


def sum_curves(*curves) -> list:
    """Point-wise sum of curves on the same grid (a None anywhere makes that point None)."""
    curves = [c for c in curves if c]
    if not curves or len({len(c) for c in curves}) != 1:
        return []
    return [(curves[0][i][0], None if any(c[i][1] is None for c in curves)
             else sum(c[i][1] for c in curves)) for i in range(len(curves[0]))]


def zero_gamma(curve: list, spot):
    """The re-priced zero-gamma level: the sign change of the curve nearest to spot (linear), or None."""
    S = _num(spot)
    pts = [(x, y) for x, y in curve or [] if y is not None]
    found = []
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if y0 == 0:
            found.append(x0)
        elif (y0 < 0) != (y1 < 0) and y1 != 0:
            found.append(x0 + (x1 - x0) * abs(y0) / (abs(y0) + abs(y1)))
    return round(min(found, key=lambda x: abs(x - S)), 2) if found and S else None


def model_gamma_ratio(inputs: list, spot, t_years):
    """Median of model gamma / Dhan gamma on the OTM side (~1.0 = IV units and time-to-expiry agree)."""
    ratios = []
    for r in inputs or []:
        dg = r["dhan_put_gamma"] if r["iv_source"] == "pe" else r["dhan_call_gamma"]
        g = bs_gamma(spot, r["strike"], r["iv"], t_years)
        if g and dg and dg > 0:
            ratios.append(g / dg)
    ratios.sort()
    return round(ratios[len(ratios) // 2], 3) if ratios else None


def repriced_profile(chains: list, spot, as_of, band_pct: float, weights: dict = None,
                     grid_pct: float = 6.0) -> dict:
    """One profile over one or more expiries [(oc, expiry)] from model gamma, plus the re-priced
    zero-gamma level and where spot sits against it. Abstains by name like `profile`."""
    S = _num(spot)
    curves, rows_sets, ivs, ratios, ts = [], [], [], [], []
    for oc, expiry in chains or []:
        t = years_to_expiry(expiry, as_of)
        inputs = strike_inputs(oc, S)
        if t is None or not inputs:
            continue
        ts.append(t)
        ivs.append(sum(1 for r in inputs if r["iv"] is not None) / len(inputs))
        ratios.append(model_gamma_ratio(inputs, S, t))
        rows_sets.append(model_strike_gex(inputs, S, t, weights))
        curves.append(gamma_curve(inputs, S, t, weights, grid_pct))
    out = profile(merge_rows(*rows_sets), S, band_pct) if rows_sets else dict(
        profile([], S, band_pct), reason="no expiry with a usable chain and time to expiry")
    curve = sum_curves(*curves)
    zg = zero_gamma(curve, S)
    at_spot = next((y for x, y in curve if abs(x - S) < 1e-6), None) if curve else None
    if at_spot is None and curve:
        at_spot = curve[len(curve) // 2][1]
    out.update(weights=dict(weights or NAIVE_WEIGHTS),
               t_years=round(min(ts), 5) if ts else None,
               iv_coverage=round(min(ivs), 3) if ivs else None,
               model_gamma_ratio=next((r for r in ratios if r is not None), None),
               zero_gamma=zg, spot_vs_zero_pct=round((S / zg - 1) * 100, 3) if zg and S else None,
               regime_at_spot=(None if at_spot is None else "positive" if at_spot > 0 else "negative"),
               zero_gamma_within_grid=zg is not None, grid_pct=grid_pct)
    return out
