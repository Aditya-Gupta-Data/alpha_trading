"""Lots by TOTAL PREMIUM, never by stop distance: the premium paid (plus the round-trip fees) is the most a
tip can lose, even if a same-day option goes to zero.

    budget = min(sandbox free cash, max_open_premium_rs − risk already open, max_per_tip_rs)
    lots   = floor((budget − round-trip fees) / (cost per share × lot size))"""
from __future__ import annotations

import math


def size(cost_ps: float, lot_size: int, n_legs: int, free_cash: float, open_risk: float, cfg: dict,
         n_tranches: int = 1, max_lots: int = None) -> dict:
    """Fees: one entry order per leg plus one exit order per leg per tranche."""
    per_lot = round(float(cost_ps) * int(lot_size), 2)
    fees = round(float(cfg["fees_per_order_rs"]) * int(n_legs) * (1 + int(n_tranches)), 2)
    room = round(float(cfg["max_open_premium_rs"]) - float(open_risk), 2)
    budget = round(max(0.0, min(float(free_cash), room, float(cfg["max_per_tip_rs"]))), 2)
    out = {"cost_per_lot_rs": per_lot, "fees_rs": fees, "budget_rs": budget, "free_cash_rs": round(free_cash, 2),
           "open_risk_rs": round(open_risk, 2), "room_rs": room, "lots": 0, "premium_total_rs": 0.0,
           "max_loss_rs": 0.0, "reason": None}
    if per_lot <= 0:
        return dict(out, reason="no positive premium to pay")
    lots = int(math.floor((budget - fees) / per_lot)) if budget > fees else 0
    if max_lots is not None:
        lots = min(lots, int(max_lots))
    if lots < 1:
        return dict(out, reason=(f"1 lot costs Rs.{per_lot:,.0f} (+Rs.{fees:,.0f} fees) but only Rs.{budget:,.0f} "
                                 f"of the sandbox is available (free cash Rs.{free_cash:,.0f}, open risk "
                                 f"Rs.{open_risk:,.0f} of Rs.{float(cfg['max_open_premium_rs']):,.0f}, "
                                 f"per-tip cap Rs.{float(cfg['max_per_tip_rs']):,.0f})"))
    premium = round(lots * per_lot, 2)
    return dict(out, lots=lots, premium_total_rs=premium, max_loss_rs=round(premium + fees, 2))


def size_tip(cost_ps: float, lot_size: int, n_legs: int, free_cash: float, open_risk: float, cfg: dict) -> dict:
    """Two tranches when two lots fit after their fees; otherwise one lot (or none)."""
    two = size(cost_ps, lot_size, n_legs, free_cash, open_risk, cfg, n_tranches=2)
    if two["lots"] >= 2:
        return dict(two, n_tranches=2)
    return dict(size(cost_ps, lot_size, n_legs, free_cash, open_risk, cfg, n_tranches=1, max_lots=1), n_tranches=1)


def split_fees(fee_per_order: float, n_legs: int, n_tranches: int) -> list:
    """Per-tranche fees: the entry orders ride on the first tranche, each tranche pays its own exit."""
    exit_fees = round(float(fee_per_order) * int(n_legs), 2)
    return [round(exit_fees * 2, 2)] + [exit_fees] * (int(n_tranches) - 1)


def tranches(lots: int) -> list:
    """[(tranche, lots, rule)]: ≥2 lots → A (half, booked at T1) + B (the rest, trailed); 1 lot → one trailed row."""
    if lots >= 2:
        a = lots // 2
        return [("A", a, "book_at_t1"), ("B", lots - a, "trail")]
    return [("ALL", lots, "trail")]


def stale(live: float, entry_low, entry_high, pct: float) -> dict:
    """The live price vs the tip's band. `stale` = more than `pct` % above the band's top."""
    if entry_high is None or live is None:
        return {"stale": False, "vs_band": None, "pct_over": None}
    top = float(entry_high)
    over = round((float(live) - top) / top * 100.0, 1) if top > 0 else None
    if over is not None and over > float(pct):
        return {"stale": True, "vs_band": "above", "pct_over": over}
    if entry_low is not None and float(live) < float(entry_low):
        return {"stale": False, "vs_band": "below", "pct_over": over}
    return {"stale": False, "vs_band": "above" if float(live) > top else "inside", "pct_over": over}
