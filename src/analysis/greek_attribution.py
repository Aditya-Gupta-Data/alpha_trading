"""
src/analysis/greek_attribution.py — second-order Taylor P&L attribution of a closed spread (decision #155, D7).

  dPnL ~ Delta*dS + 0.5*Gamma*dS^2 + Theta*dt + Vega*dsigma
         + 0.5*Volga*dsigma^2 + Vanna*dS*dsigma + Charm*dS*dt

Per leg, per step between consecutive end-of-day chain captures (data/lake/chains) inside the hold, signed by
side and scaled by quantity. Greeks are Black-Scholes from each strike's OTM IV (`gamma_profile.strike_inputs`;
Dhan's own EOD Greeks are zero on ITM calls). Units: dS in rupees, dt in calendar days, dsigma in IV points.
Coverage floor (D7): a step counts only when Delta, Gamma, Theta and Vega are all computable for every leg;
Volga/Vanna/Charm come from finite differences and fall back to 0 when they cannot be formed.
The residual is the actual mark change minus the seven terms. Advisory only; never read by sizing or exits.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime

from src import lake
from src.analysis import gamma_profile as gp

LAKE_ROOT = None                                  # None = lake.LAKE_ROOT (tests point it at tmp)
TABLE = "greek_attribution"
COMPONENTS = ("delta", "gamma", "theta", "vega", "volga", "vanna", "charm")
PRIMARY = ("delta", "gamma", "theta", "vega")
_VOL_H = 0.5                                      # IV points, finite-difference step for vanna/volga
_DAY = 1.0 / 365.0


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _d1d2(S, K, iv_pct, t, r):
    sigma = iv_pct / 100.0
    root = sigma * math.sqrt(t)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * t) / root
    return d1, d1 - root, sigma


def _valid(S, K, iv_pct, t):
    return all(isinstance(x, (int, float)) and x > 0 for x in (S, K, iv_pct, t))


def bs_price(S, K, iv_pct, t, option_type, r=gp.RISK_FREE):
    if not _valid(S, K, iv_pct, t):
        return None
    d1, d2, _ = _d1d2(S, K, iv_pct, t, r)
    disc = K * math.exp(-r * t)
    if str(option_type).upper() == "CE":
        return S * _ncdf(d1) - disc * _ncdf(d2)
    return disc * _ncdf(-d2) - S * _ncdf(-d1)


def _delta(S, K, iv_pct, t, ot, r):
    d1, _, _ = _d1d2(S, K, iv_pct, t, r)
    return _ncdf(d1) if ot == "CE" else _ncdf(d1) - 1.0


def _vega_pt(S, K, iv_pct, t, r):
    d1, _, _ = _d1d2(S, K, iv_pct, t, r)
    return S * _npdf(d1) * math.sqrt(t) / 100.0


def bs_greeks(S, K, iv_pct, t, option_type, r=gp.RISK_FREE) -> dict | None:
    """Per share, long one option: delta, gamma, theta (per day), vega (per IV point), vanna (delta per IV
    point), volga (vega per IV point, per point), charm (delta per day). None when the primary four can't be formed."""
    if not _valid(S, K, iv_pct, t):
        return None
    ot = "CE" if str(option_type).upper() == "CE" else "PE"
    d1, d2, sigma = _d1d2(S, K, iv_pct, t, r)
    sq = math.sqrt(t)
    disc = K * math.exp(-r * t)
    decay = -S * _npdf(d1) * sigma / (2 * sq)
    theta_y = decay - r * disc * _ncdf(d2) if ot == "CE" else decay + r * disc * _ncdf(-d2)
    out = {"delta": _delta(S, K, iv_pct, t, ot, r), "gamma": _npdf(d1) / (S * sigma * sq),
           "theta": theta_y / 365.0, "vega": _vega_pt(S, K, iv_pct, t, r),
           "vanna": 0.0, "volga": 0.0, "charm": 0.0}
    lo, hi = iv_pct - _VOL_H, iv_pct + _VOL_H
    if lo > 0:
        out["vanna"] = (_delta(S, K, hi, t, ot, r) - _delta(S, K, lo, t, ot, r)) / (2 * _VOL_H)
        out["volga"] = (_vega_pt(S, K, hi, t, r) - _vega_pt(S, K, lo, t, r)) / (2 * _VOL_H)
    step = min(_DAY, t / 2)
    out["charm"] = (_delta(S, K, iv_pct, t - step, ot, r) - out["delta"]) / step * _DAY
    return out


def taylor_terms(g: dict, dS: float, dt_days: float, dsig: float) -> dict:
    """The seven terms of the second-order expansion for one long option (per share)."""
    return {"delta": g["delta"] * dS, "gamma": 0.5 * g["gamma"] * dS * dS,
            "theta": g["theta"] * dt_days, "vega": g["vega"] * dsig,
            "volga": 0.5 * (g.get("volga") or 0.0) * dsig * dsig,
            "vanna": (g.get("vanna") or 0.0) * dS * dsig,
            "charm": (g.get("charm") or 0.0) * dS * dt_days}


def _mark(node: dict):
    """A leg's EOD mark: the bid/ask mid when both sides quote, else the last price."""
    node = node or {}
    bid, ask = gp._num(node.get("top_bid_price")), gp._num(node.get("top_ask_price"))
    if bid and ask and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    ltp = gp._num(node.get("last_price"))
    return ltp if ltp and ltp > 0 else None


def _ts(row: dict, day: str):
    try:
        t = datetime.fromisoformat(str(row.get("captured_at")))
    except (TypeError, ValueError):
        t = datetime.fromisoformat(f"{day}T15:40:00")
    return t if t.tzinfo else t.replace(tzinfo=gp._IST)


def _leg_state(row: dict, leg: dict, expiry, day: str):
    """(spot, iv, mark, t_years) for one leg on one capture, or None."""
    spot = gp._num(row.get("spot"))
    k = gp._num(leg.get("strike"))
    node = ((row.get("oc") or {}).get(f"{k:.6f}") or {}).get(str(leg.get("option_type")).lower()) if k else None
    ivs = {r["strike"]: r["iv"] for r in gp.strike_inputs(row.get("oc") or {}, spot)} if spot else {}
    t = gp.years_to_expiry(expiry, _ts(row, day).isoformat())
    iv, mark = ivs.get(k), _mark(node)
    return None if None in (spot, iv, mark, t) else (spot, iv, mark, t)


def attribute(spread: dict, days: list) -> dict:
    """`days` = [(day, chain_row)] for the spread's expiry, chronological. Sums the seven terms over every
    covered step; reports coverage, the actual window P&L from marks, and the residual."""
    legs = (spread or {}).get("legs") or []
    qty = int(spread.get("lot_size") or 0) * int(spread.get("lots") or 1)
    expiry = spread.get("expiry")
    comp = {c: 0.0 for c in COMPONENTS}
    out = {"method": "BS greeks from OTM IV; 2nd-order Taylor (#155 D7)", "steps": max(0, len(days) - 1),
           "covered": 0, "coverage": None, "components": None, "taylor_total": None,
           "actual_window_pnl": None, "residual": None, "residual_share": None,
           "window": [days[0][0], days[-1][0]] if days else None, "reason": None}
    if not legs or qty <= 0:
        out["reason"] = "spread has no legs or quantity"
        return out
    if len(days) < 2:
        out["reason"] = "fewer than two end-of-day chain captures inside the hold"
        return out
    actual = 0.0
    for (d0, r0), (d1, r1) in zip(days, days[1:]):
        dt = (_ts(r1, d1) - _ts(r0, d0)).total_seconds() / 86400.0
        step_terms, step_actual, ok = {c: 0.0 for c in COMPONENTS}, 0.0, True
        for leg in legs:
            a, b = _leg_state(r0, leg, expiry, d0), _leg_state(r1, leg, expiry, d1)
            g = bs_greeks(a[0], leg["strike"], a[1], a[3], leg["option_type"]) if a else None
            if not a or not b or not g or any(g.get(k) is None for k in PRIMARY):
                ok = False
                break
            sign = 1.0 if str(leg.get("side")).upper() == "BUY" else -1.0
            for c, v in taylor_terms(g, b[0] - a[0], dt, b[1] - a[1]).items():
                step_terms[c] += sign * qty * v
            step_actual += sign * qty * (b[2] - a[2])
        if not ok:
            continue
        out["covered"] += 1
        actual += step_actual
        for c in COMPONENTS:
            comp[c] += step_terms[c]
    out["coverage"] = round(out["covered"] / out["steps"], 3)
    if not out["covered"]:
        out["reason"] = "no step had Delta, Gamma, Theta and Vega for every leg (D7 floor)"
        return out
    total = sum(comp.values())
    out.update(components={c: round(v, 2) for c, v in comp.items()}, taylor_total=round(total, 2),
               actual_window_pnl=round(actual, 2), residual=round(actual - total, 2),
               residual_share=round((actual - total) / abs(actual), 4) if actual else None)
    return out


def _slug(ticker: str):
    from src.ingestion import chain_archiver as ca
    t = str(ticker or "")
    return ca.UNDERLYINGS.get(t) or ca.extension_slug(t[:-3] if t.upper().endswith(".NS") else t)


def chain_days(ticker, expiry, start, end, root=None) -> list:
    """[(day, chain_row)] of the archived captures for (ticker, expiry) with start <= day <= end."""
    slug = _slug(ticker)
    root = root if root is not None else LAKE_ROOT
    out = []
    for day, row in lake.scan(f"chains/{slug}", start=str(start)[:10], end=str(end)[:10], root=root):
        if str(row.get("expiry"))[:10] == str(expiry)[:10] and isinstance(row.get("oc"), dict):
            out.append((day, row))
    return out


def attribute_entry(entry: dict, root=None) -> dict:
    """The attribution for one resolved journal entry, plus the trade's booked P&L and what fell outside the
    archived window (the entry-day and exit-day moves the end-of-day captures cannot see)."""
    spread = entry.get("spread") or {}
    outcome = entry.get("outcome") or {}
    end = outcome.get("exit_date") or outcome.get("settled_at") or date.today().isoformat()
    res = attribute(spread, chain_days(entry.get("ticker"), spread.get("expiry"), entry.get("date"), end, root))
    pnl = gp._num(outcome.get("pnl_rs"))
    res.update(trade_pnl=pnl, outside_window=(round(pnl - res["actual_window_pnl"], 2)
                                              if pnl is not None and res["actual_window_pnl"] is not None else None))
    return res


_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    journal_ref TEXT PRIMARY KEY, as_of TEXT NOT NULL, ticker TEXT, strategy TEXT,
    coverage REAL, components TEXT, taylor_total REAL, actual_window_pnl REAL, residual REAL,
    trade_pnl REAL, outside_window REAL, method TEXT, reason TEXT
);
"""


def record(conn, entry: dict, result: dict) -> bool:
    """One row per journal ref, first write wins (re-runs are no-ops; the outcomes ledger is never touched)."""
    from src import brain_map
    conn.executescript(_SCHEMA)
    cur = conn.execute(
        f"INSERT OR IGNORE INTO {TABLE} (journal_ref, as_of, ticker, strategy, coverage, components, taylor_total, "
        "actual_window_pnl, residual, trade_pnl, outside_window, method, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (brain_map.journal_ref_for(entry), datetime.now(gp._IST).isoformat(timespec="seconds"), entry.get("ticker"),
         (entry.get("spread") or {}).get("strategy"), result.get("coverage"),
         json.dumps(result.get("components")) if result.get("components") else None,
         result.get("taylor_total"), result.get("actual_window_pnl"), result.get("residual"),
         result.get("trade_pnl"), result.get("outside_window"), result.get("method"), result.get("reason")))
    conn.commit()
    return bool(cur.rowcount)
