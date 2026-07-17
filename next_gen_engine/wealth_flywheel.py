"""
next_gen_engine/wealth_flywheel.py — sweep advisory -> concrete paper order
============================================================================

Blueprint Phase 1 (owner, 2026-07-17). ⚠️ EXTENSION, NOT A REPLACEMENT:
the canonical `src/wealth_lock.py` ALREADY implements the 50% GOLDBEES
profit sweep as a LEDGER + Discord card (sweep_ledger table, decision from
the 2026-07-10 scratchpad build). What it deliberately stopped short of —
because GOLDBEES had no scrip-master-verified id yet — is turning the
earmarked rupees into an actual sized PAPER ORDER. This module is exactly
that missing step, kept pure so it can be merged into
wealth_lock.sweep_on_settlement at deploy time.

    net winning P&L  ->  50% earmark  ->  {qty, limit, notional} paper buy

Contract:
  * PAPER ONLY: the order dict carries mode="PAPER" and is a proposal
    object for the ledger/journal — nothing here routes anywhere.
  * Whole units only (GOLDBEES trades in units); the un-investable
    remainder is reported honestly as `cash_residual_rs`, never rounded
    into the order.
  * A missing/zero ETF price returns an order-less earmark (same posture
    as wealth_lock's mock_units=NULL) — the sweep is still recorded, the
    sizing waits for a real quote.
  * CANONICAL MERGE TARGET: src/wealth_lock.py (this file's build_sweep_order
    slots in where record_sweep computes mock_units today). GOLDBEES must
    be added to dhan_client.SECURITY_ID_MAP with a scrip-master-verified id
    before the live quote path exists.
"""
import math

SWEEP_PCT = 50.0                 # match wealth_lock.SWEEP_PCT — one number, two files is a merge-time TODO
SWEEP_INSTRUMENT = "GOLDBEES"


def sweep_amount(pnl_net: float, sweep_pct: float = SWEEP_PCT) -> float:
    """Rupees earmarked from a CLOSED WINNING trade. Zero for losses,
    breakeven, or junk input — the flywheel only spins on real profit."""
    if not isinstance(pnl_net, (int, float)) or pnl_net <= 0:
        return 0.0
    return round(pnl_net * sweep_pct / 100.0, 2)


def build_sweep_order(pnl_net: float, etf_price: float = None,
                      sweep_pct: float = SWEEP_PCT,
                      instrument: str = SWEEP_INSTRUMENT) -> dict | None:
    """The mechanical sweep: 50% of net profit -> a sized paper buy order.

    Returns None when there is nothing to sweep (non-winning trade).
    Returns an earmark WITHOUT an order (order=None) when the ETF price is
    unknown or the earmark can't buy one whole unit — honest abstention,
    the rupee amount is still recorded for a later batch sweep."""
    earmark = sweep_amount(pnl_net, sweep_pct)
    if earmark <= 0:
        return None
    base = {"instrument": instrument, "sweep_pct": sweep_pct,
            "pnl_net": round(float(pnl_net), 2), "earmark_rs": earmark,
            "mode": "PAPER"}
    if not etf_price or etf_price <= 0:
        return {**base, "order": None,
                "cash_residual_rs": earmark,
                "note": "no usable ETF quote — earmark recorded, sizing "
                        "deferred"}
    qty = math.floor(earmark / etf_price)
    if qty < 1:
        return {**base, "order": None,
                "cash_residual_rs": earmark,
                "note": f"earmark below one unit of {instrument} "
                        f"@ {etf_price} — accumulates for a later sweep"}
    notional = round(qty * etf_price, 2)
    return {
        **base,
        "order": {"symbol": instrument, "side": "buy", "qty": qty,
                  "limit_price": etf_price, "notional_rs": notional,
                  "mode": "PAPER"},
        "cash_residual_rs": round(earmark - notional, 2),
    }
