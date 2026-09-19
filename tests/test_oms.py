"""
Phase M2 OMS plumbing (decision #100, GAP 3): trade_tickets / trade_legs /
leg_events schema, the leg state machine, ticket roll-up, and the
strategy_router that turns a proposal into a multi-leg Order Ticket.

Offline; in-memory sqlite; no broker, no quotes. Run:
    python -m pytest tests/test_oms.py -q
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src import oms, strategy_router as sr
from src.strategy import StrategyConstructor

ROOT = Path(__file__).resolve().parent.parent


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    oms.ensure_schema(c)
    return c


def _proposal(strategy="bull_call_spread", lots=2):
    sc = StrategyConstructor(vix=13.0, lot_size=65)
    if strategy == "bull_call_spread":
        spread = sc.construct_bull_call_spread(25000, 25200, 100.0, 30.0)
    else:
        spread = sc.construct_iron_condor(24800, 25200, 200, 95, 38, 100, 40)
    spread = dict(spread, lots=lots, expiry="2026-09-30")
    for leg in spread["legs"]:
        leg["fill_basis"] = "quoted"
    return {"ticker": "NIFTY 50", "short_id": "abc12345", "signal": "test", "spread": spread}


# ------------------------------------------------------------ router

def test_routing_table_is_the_one_source_for_direction_and_family():
    from src import exposure_gate as eg
    from src.strategies import glassbreaking as gb
    assert eg.DIRECTION_BY_STRATEGY == {k: v["direction"] for k, v in sr.ROUTING_TABLE.items()}
    assert gb.DEFINED_RISK_STRUCTURES == frozenset(sr.ROUTING_TABLE)
    assert sr.direction_of("bear_put_spread") == "bearish" and sr.direction_of("x") is None
    assert sr.family_of("iron_condor") == "range_bound" and sr.family_of("x") == "directional"
    assert sr.structure_for_view("bullish") == "bull_call_spread"
    assert sr.structure_for_view("neutral", 13.0, 14.5) == "iron_condor"
    assert sr.structure_for_view("neutral", 15.0, 14.5) == "iron_butterfly"
    assert all(sr.is_defined_risk(k) for k in sr.ROUTING_TABLE) and not sr.is_defined_risk("naked_put")


def test_ticket_is_pure_legs_are_worked_longs_first_and_ids_are_deterministic():
    p = _proposal("iron_condor", lots=1)
    t1 = sr.build_ticket(p, issued_at="2026-09-19T10:00:00+05:30")
    t2 = sr.build_ticket(p, issued_at="2026-09-19T10:00:00+05:30")
    assert t1 == t2                                              # no I/O, no clock
    assert t1["ticket_id"].startswith("tkt:") and t1["direction"] == "neutral"
    sides = [l["side"] for l in t1["legs"]]
    assert sides == ["BUY", "BUY", "SELL", "SELL"]               # protective legs first
    assert all(l["qty_target"] == 65 for l in t1["legs"])
    assert [l["leg_index"] for l in t1["legs"]] == [0, 1, 2, 3]
    cids = [l["correlation_id"] for l in t1["legs"]]
    assert len(set(cids)) == 4 and all(len(c) <= oms.CORRELATION_ID_MAX for c in cids)
    assert all(c.replace("_", "").replace("-", "").isalnum() for c in cids)
    assert t1["reward_risk"] == pytest.approx(117 / 83, abs=1e-3)


# ------------------------------------------------------------ state machine

def test_issue_is_idempotent_and_every_leg_starts_pending():
    conn = _conn()
    r = sr.issue(conn, _proposal(), issued_at="2026-09-19T10:00:00+05:30")
    assert r["ok"] and r["created"]
    again = sr.issue(conn, _proposal(), issued_at="2026-09-19T10:00:00+05:30")
    assert again["ok"] and not again["created"]
    v = oms.ticket_view(conn, r["ticket_id"])
    assert v["status"] == oms.PENDING and [l["state"] for l in v["legs"]] == [oms.PENDING] * 2
    assert [e["to_state"] for e in v["events"]] == [oms.PENDING] * 2
    assert conn.execute("SELECT COUNT(*) FROM trade_legs").fetchone()[0] == 2


def test_partial_then_full_fill_with_volume_weighted_average():
    conn = _conn()
    tid = sr.issue(conn, _proposal(lots=2))["ticket_id"]         # 130 per leg
    leg = f"{tid}:0"
    r = oms.apply_fill(conn, leg, 65, 100.0, broker_order_id="B1")
    assert r == {"ok": True, "state": oms.PARTIAL, "qty_filled": 65, "avg_fill_price": 100.0}
    assert oms.ticket_status(conn, tid) == oms.PARTIAL
    r = oms.apply_fill(conn, leg, 65, 102.0)
    assert r["state"] == oms.FILLED and r["avg_fill_price"] == 101.0
    row = oms.ticket_view(conn, tid)["legs"][0]
    assert row["broker_order_id"] == "B1" and row["qty_filled"] == 130
    assert oms.ticket_status(conn, tid) == oms.PARTIAL              # leg 1 still open
    oms.apply_fill(conn, f"{tid}:1", 130, 30.0)
    assert oms.ticket_status(conn, tid) == oms.FILLED
    assert oms.open_legs(conn, tid) == []


def test_over_fill_bad_numbers_and_terminal_fills_are_refused():
    conn = _conn()
    tid = sr.issue(conn, _proposal(lots=1))["ticket_id"]
    leg = f"{tid}:0"
    assert oms.apply_fill(conn, leg, 66, 100.0)["reason"].startswith("over-fill")
    assert not oms.apply_fill(conn, leg, 0, 100.0)["ok"]
    assert not oms.apply_fill(conn, leg, 10, -1.0)["ok"]
    assert not oms.apply_fill(conn, leg, "x", 1.0)["ok"]
    assert not oms.apply_fill(conn, "nope", 1, 1.0)["ok"]
    oms.apply_fill(conn, leg, 65, 100.0)
    r = oms.apply_fill(conn, leg, 1, 100.0)
    assert not r["ok"] and "terminal" in r["reason"] and r["state"] == oms.FILLED
    assert conn.execute("SELECT qty_filled FROM trade_legs WHERE leg_id = ?", (leg,)).fetchone()[0] == 65


def test_reject_only_from_pending_and_it_taints_the_ticket():
    conn = _conn()
    tid = sr.issue(conn, _proposal(lots=1))["ticket_id"]
    oms.apply_fill(conn, f"{tid}:0", 65, 100.0)                  # leg 0 FILLED
    r = oms.reject(conn, f"{tid}:1", "DH-906 insufficient margin", broker_order_id="B9")
    assert r == {"ok": True, "state": oms.REJECTED}
    assert oms.ticket_status(conn, tid) == oms.REJECTED
    v = oms.ticket_view(conn, tid)
    assert v["legs"][1]["reject_reason"].startswith("DH-906") and v["legs"][1]["broker_order_id"] == "B9"
    # a filled leg cannot be rejected; a partial leg cannot be rejected either
    assert not oms.reject(conn, f"{tid}:0", "late")["ok"]
    tid2 = sr.issue(conn, _proposal(lots=2), issued_at="2026-09-19T11:00:00+05:30")["ticket_id"]
    oms.apply_fill(conn, f"{tid2}:0", 65, 100.0)
    assert "illegal PARTIAL -> REJECTED" in oms.reject(conn, f"{tid2}:0", "x")["reason"]


def test_cancel_keeps_partial_fills_and_rolls_up_honestly():
    conn = _conn()
    tid = sr.issue(conn, _proposal(lots=2))["ticket_id"]
    oms.apply_fill(conn, f"{tid}:0", 65, 100.0)
    r = oms.cancel_ticket(conn, tid, "market closed")
    assert r["status"] == oms.PARTIAL                              # a fill exists, so not CANCELLED
    v = oms.ticket_view(conn, tid)
    assert [l["state"] for l in v["legs"]] == [oms.CANCELLED, oms.CANCELLED]
    assert v["legs"][0]["qty_filled"] == 65                       # never reduced
    assert r["legs"][f"{tid}:0"]["qty_filled"] == 65
    # cancelling again is a no-op on terminal legs
    r2 = oms.cancel_ticket(conn, tid)
    assert all(x.get("skipped") == "terminal" for x in r2["legs"].values())
    # a ticket with no fills at all rolls up to CANCELLED
    tid2 = sr.issue(conn, _proposal(lots=1), issued_at="2026-09-19T12:00:00+05:30")["ticket_id"]
    assert oms.cancel_ticket(conn, tid2)["status"] == oms.CANCELLED


def test_roll_up_matrix():
    P, A, F, R, C = oms.PENDING, oms.PARTIAL, oms.FILLED, oms.REJECTED, oms.CANCELLED
    assert oms.roll_up_status([], []) == P
    assert oms.roll_up_status([P, P], [0, 0]) == P
    assert oms.roll_up_status([F, F], [1, 1]) == F
    assert oms.roll_up_status([F, R], [1, 0]) == R
    assert oms.roll_up_status([A, P], [1, 0]) == A
    assert oms.roll_up_status([F, P], [1, 0]) == A
    assert oms.roll_up_status([C, C], [0, 0]) == C
    assert oms.roll_up_status([C, C], [5, 0]) == A                # cancelled with a fill = partial
    assert oms.roll_up_status([F, C], [1, 0]) == A                # incomplete, some fill


def test_legal_transitions_are_exactly_the_documented_machine():
    assert oms.LEGAL_TRANSITIONS[oms.PENDING] == {oms.PARTIAL, oms.FILLED, oms.REJECTED, oms.CANCELLED}
    assert oms.LEGAL_TRANSITIONS[oms.PARTIAL] == {oms.PARTIAL, oms.FILLED, oms.CANCELLED}
    for s in oms.TERMINAL:
        assert oms.LEGAL_TRANSITIONS[s] == set()
    assert set(oms.STATES) == {"PENDING", "PARTIAL", "FILLED", "REJECTED", "CANCELLED"}


def test_audit_trail_is_append_only_and_complete():
    conn = _conn()
    tid = sr.issue(conn, _proposal(lots=1))["ticket_id"]
    oms.apply_fill(conn, f"{tid}:0", 30, 100.0)
    oms.apply_fill(conn, f"{tid}:0", 35, 101.0)
    oms.reject(conn, f"{tid}:1", "no")
    ev = oms.ticket_view(conn, tid)["events"]
    assert [(e["from_state"], e["to_state"], e["qty"]) for e in ev] == [
        (None, "PENDING", None), (None, "PENDING", None),
        ("PENDING", "PARTIAL", 30), ("PARTIAL", "FILLED", 35), ("PENDING", "REJECTED", None)]
    assert conn.execute("SELECT COUNT(*) FROM leg_events").fetchone()[0] == 5


def test_schema_is_additive_and_coexists_with_brain_map():
    from src import brain_map
    conn = brain_map.connect(":memory:")
    oms.ensure_schema(conn)
    oms.ensure_schema(conn)                                        # twice is fine
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"trade_tickets", "trade_legs", "leg_events", "outcomes", "events"} <= tables


def test_the_oms_places_nothing_and_is_on_no_live_path():
    """Rule 7 stays: no broker import, no quote read, no journal/treasury
    write, and nothing in the live modules calls the router's write door."""
    for name in ("src/oms.py", "src/strategy_router.py"):
        src = (ROOT / name).read_text()
        for forbidden in ("dhanhq", "place_order", "journal.log(", "firm_treasury",
                          "portfolio_manager", "request_entry", "get_option_chain",
                          "get_live_price", "requests.", "httpx"):
            assert forbidden not in src, (name, forbidden)
    for live in ("src/options_proposer.py", "src/plan_tracker.py", "src/market_loop.py",
                 "src/live_bridge.py", "src/equity_desk.py"):
        assert "strategy_router.issue" not in (ROOT / live).read_text(), live
        assert "oms.issue_ticket" not in (ROOT / live).read_text(), live
