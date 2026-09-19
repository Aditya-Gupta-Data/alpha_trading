"""
src/strategy_router.py — ONE routing table, and Order Tickets instead of instant fills
=======================================================================================

WHY (decision #100, 2026-09-19; SYSTEM_BLUEPRINT.md GAP 3). Structure routing
lived in two places that had to be edited in step — the `if/elif` chain in
`options_proposer.build_proposal` and its unwired twin
`trade_planner.map_technical_to_strategy` — plus a third map in
`exposure_gate.DIRECTION_BY_STRATEGY`, plus the per-primitive map in
`strategies.glassbreaking.STRUCTURES`. This module is the single table those
consult, and it is also where a BUILT structure becomes an ORDER TICKET: a
multi-leg record of intent with a lifecycle (`src/oms.py`), not a spread
that is assumed to have filled whole at top-of-book the instant it was
proposed.

WHAT IT DOES NOT DO. It does not place an order, read a quote, or touch the
capital layer. `build_proposal` still builds and gates exactly as before;
`build_ticket` is a pure function of the proposal it already produces, and
`issue` writes the ticket to the OMS tables only when a caller asks. Nothing
on the live path calls `issue` yet — wiring it is the M2 sequence, gated on
the court's read of the paper OMS (HANDOVER). Rule 7 is unchanged.

THE TABLE. Every row is a defined-risk structure with its direction, its
reward-to-risk family, whether it is a credit or debit structure, and the
leg order a real venue should work them in (protective long legs FIRST, so
no moment exists where a short option is naked — the sequencing
`next_gen_engine/execution_algo.sequence_spread_legs` designed).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src import oms

IST = timezone(timedelta(hours=5, minutes=30))

# strategy -> routing facts. `family` picks the reward-to-risk floor
# (strategy.reward_risk_floor); `premium` says which way money moves at
# entry; `leg_order` is the working order for a venue.
ROUTING_TABLE = {
    "bull_call_spread": {"direction": "bullish", "family": "directional",
                         "premium": "debit", "defined_risk": True},
    "bear_put_spread":  {"direction": "bearish", "family": "directional",
                         "premium": "debit", "defined_risk": True},
    "iron_condor":      {"direction": "neutral", "family": "range_bound",
                         "premium": "credit", "defined_risk": True},
    "iron_butterfly":   {"direction": "neutral", "family": "range_bound",
                         "premium": "credit", "defined_risk": True},
}

# view -> the structure the desk trades for it (the proposer's chain, as one row)
STRUCTURE_FOR_VIEW = {"bullish": "bull_call_spread",
                      "bearish": "bear_put_spread",
                      "neutral": "iron_condor"}          # iron_butterfly when VIX is in the upper band


def direction_of(strategy: str):
    return (ROUTING_TABLE.get(strategy) or {}).get("direction")


def family_of(strategy: str) -> str:
    return (ROUTING_TABLE.get(strategy) or {}).get("family", "directional")


def structure_for_view(view: str, vix: float = None, butterfly_min_vix: float = None) -> str | None:
    """The structure the desk builds for a directional/neutral read. Neutral
    takes the butterfly only in the upper half of the tradeable band."""
    if view == "neutral" and vix is not None and butterfly_min_vix is not None \
            and vix >= butterfly_min_vix:
        return "iron_butterfly"
    return STRUCTURE_FOR_VIEW.get(view)


def is_defined_risk(strategy: str) -> bool:
    return bool((ROUTING_TABLE.get(strategy) or {}).get("defined_risk"))


# ------------------------------------------------------------------ tickets

def leg_working_order(legs: list) -> list:
    """Protective LONG legs first, then the shorts — never a naked short at
    any instant of working a spread. Stable within each group."""
    buys = [l for l in legs if str(l.get("side", "")).upper() == "BUY"]
    sells = [l for l in legs if str(l.get("side", "")).upper() != "BUY"]
    return buys + sells


def build_ticket(proposal: dict, source: str = "options_proposer",
                 issued_at: str = None, journal_ref: str = None,
                 account_id: str = None, lots: int = None) -> dict:
    """A pure Order Ticket from a proposal dict of the shape
    `options_proposer.build_proposal` returns (`proposal["spread"]` with
    legs/lots/lot_size/expiry, `proposal["ticker"]`). No I/O.

    `account_id` / `lots` (decision #102): a SHADOW paper account's ticket
    for the same journal_ref — its own id, its own (smaller) lot count,
    the same legs and limits."""
    spread = proposal["spread"]
    lots = int(lots if lots is not None else (spread.get("lots") or 1))
    lot_size = int(spread["lot_size"])
    strategy = spread["strategy"]
    issued_at = issued_at or datetime.now(IST).isoformat(timespec="seconds")
    account_id = account_id or oms.PRIMARY_ACCOUNT
    tid = oms.ticket_id_for(journal_ref or proposal.get("short_id"),
                           proposal["ticker"], strategy, issued_at, account_id=account_id)
    legs = []
    for i, leg in enumerate(leg_working_order(spread["legs"])):
        legs.append({
            "leg_id": f"{tid}:{i}",
            "leg_index": i,
            "side": str(leg["side"]).upper(),
            "option_type": leg.get("option_type"),
            "strike": leg.get("strike"),
            "expiry": spread.get("expiry"),
            "qty_target": lots * lot_size,
            "limit_price": leg.get("premium"),
            "fill_basis": leg.get("fill_basis"),
            "correlation_id": oms.correlation_id_for(tid, i),
        })
    return {"ticket_id": tid, "journal_ref": journal_ref or proposal.get("short_id"),
            "underlying": proposal["ticker"], "strategy": strategy,
            "direction": spread.get("direction") or direction_of(strategy),
            "lots": lots, "lot_size": lot_size,
            "reward_risk": spread.get("reward_risk"),
            "account_id": account_id,
            "source": source, "issued_at": issued_at,
            "note": (proposal.get("signal") or "")[:200],
            "legs": legs}


def build_exit_ticket(entry: dict, leg_limits: dict, resolution: str,
                      source: str = "plan_tracker", issued_at: str = None,
                      account_id: str = None, lots: int = None) -> dict:
    """The EXIT Order Ticket for an open journal entry (decision #103): the
    same legs with every side FLIPPED (long legs sold, short legs bought
    back), worked as one atomic basket, each leg's limit = the exit
    premium the caller priced it at (`leg_limits[(strike, 'CE'/'PE')]` —
    the tracker's modeled close or the intraday chain's real quote).
    `resolution` rides on the note. Pure; no I/O."""
    spread = entry["spread"]
    lots = int(lots if lots is not None else (spread.get("lots") or 1))
    lot_size = int(spread["lot_size"])
    strategy = spread["strategy"]
    issued_at = issued_at or datetime.now(IST).isoformat(timespec="seconds")
    account_id = account_id or oms.PRIMARY_ACCOUNT
    ref = entry.get("short_id")
    tid = oms.ticket_id_for(ref, entry["ticker"], strategy, issued_at,
                           account_id=account_id, kind=oms.EXIT)
    flipped = []
    for leg in spread["legs"]:
        side = "SELL" if str(leg["side"]).upper() == "BUY" else "BUY"
        key = (float(leg["strike"]), str(leg.get("option_type") or "").upper())
        flipped.append(dict(leg, side=side, premium=leg_limits.get(key)))
    legs = []
    for i, leg in enumerate(leg_working_order(flipped)):
        legs.append({
            "leg_id": f"{tid}:{i}",
            "leg_index": i,
            "side": leg["side"],
            "option_type": leg.get("option_type"),
            "strike": leg.get("strike"),
            "expiry": spread.get("expiry"),
            "qty_target": lots * lot_size,
            "limit_price": leg.get("premium"),
            "fill_basis": "exit_limit",
            "correlation_id": oms.correlation_id_for(tid, i),
        })
    return {"ticket_id": tid, "journal_ref": ref, "underlying": entry["ticker"],
            "strategy": strategy,
            "direction": spread.get("direction") or direction_of(strategy),
            "lots": lots, "lot_size": lot_size, "reward_risk": spread.get("reward_risk"),
            "account_id": account_id, "kind": oms.EXIT,
            "source": source, "issued_at": issued_at,
            "note": f"EXIT {resolution}"[:200], "legs": legs}


def issue_exit(conn, entry: dict, leg_limits: dict, resolution: str, **kw) -> dict:
    """Persist an EXIT ticket (every leg PENDING). The tracker's one write
    door into the OMS on the way OUT (decision #103)."""
    ticket = build_exit_ticket(entry, leg_limits, resolution, **kw)
    res = oms.issue_ticket(conn, ticket)
    return {**res, "ticket": ticket}


def issue(conn, proposal: dict, **kw) -> dict:
    """Build and persist a ticket (every leg PENDING). The only write door
    from a proposal into the OMS. Not on any live path."""
    ticket = build_ticket(proposal, **kw)
    res = oms.issue_ticket(conn, ticket)
    return {**res, "ticket": ticket}
