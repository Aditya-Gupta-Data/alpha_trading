"""
Phase M2 paper execution (decision #101): the paper venue drives Order
Tickets through PENDING -> FILLED with liquidity-tier slippage, rolls the
ticket up through the OMS, and — when the flag is on — an APPROVED options
entry is issued and filled through it, with the venue's prices stamped
onto the journal legs. Also the tier-1 id-universe extension.

Offline: in-memory brain_map, fake journal, injected slippage. Run:
    python -m pytest tests/test_paper_venue.py -q
"""

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src import oms, strategy_router as sr
from src.execution import paper_venue as pv
from src.strategy import StrategyConstructor

ROOT = Path(__file__).resolve().parent.parent


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    oms.ensure_schema(c)
    return c


def _proposal(lots=1, ticker="NIFTY 50"):
    sc = StrategyConstructor(vix=13.0, lot_size=65)
    spread = dict(sc.construct_bull_call_spread(25000, 25200, 100.0, 30.0),
                  lots=lots, expiry="2026-09-30")
    for leg in spread["legs"]:
        leg["fill_basis"] = "quoted"
    return {"ticker": ticker, "short_id": "sp000001", "signal": "t", "spread": spread}


# ------------------------------------------------------------ pricing

def test_slipped_price_worsens_each_side_and_respects_the_tick():
    assert pv.slipped_price(100.0, "BUY", 0.001) == 100.1
    assert pv.slipped_price(100.0, "SELL", 0.001) == 99.9
    assert pv.slipped_price(0.03, "SELL", 0.5) == 0.05


def test_tier_fraction_comes_from_the_liquidity_ladder(monkeypatch):
    from src import liquidity_slippage as ls
    seen = []
    monkeypatch.setattr(ls, "tier_slippage_frac", lambda sym, path=None: seen.append(sym) or 0.0025)
    assert pv._tier_frac("DIXON.NS") == 0.0025 and seen == ["DIXON"]
    assert pv._tier_frac("NIFTY 50", slippage_fn=lambda u: 0.001) == 0.001


# ------------------------------------------------------------ the venue

def test_sweep_fills_every_pending_leg_with_slippage_and_rolls_the_ticket_up():
    conn = _conn()
    tid = sr.issue(conn, _proposal(lots=2), issued_at="2026-09-19T10:00:00+05:30")["ticket_id"]
    s = pv.sweep(conn, today=date(2026, 9, 19), slippage_fn=lambda u: 0.001, stamp=False)
    assert s["legs"] == 2 and s["filled"] == 2 and s["rejected"] == 0 and s["refused"] == 0
    assert s["tickets_filled"] == [tid]
    v = oms.ticket_view(conn, tid)
    assert v["status"] == oms.FILLED
    buy, sell = v["legs"]                                  # longs first
    assert buy["side"] == "BUY" and buy["avg_fill_price"] == 100.1 and buy["qty_filled"] == 130
    assert sell["side"] == "SELL" and sell["avg_fill_price"] == 29.97
    assert buy["fill_basis"] == "venue" and buy["broker_order_id"] == "paper_venue:2026-09-19"
    assert [e["to_state"] for e in v["events"]] == ["PENDING", "PENDING", "FILLED", "FILLED"]


def test_partial_fills_leave_the_ticket_partial_until_the_next_sweep():
    conn = _conn()
    tid = sr.issue(conn, _proposal(lots=2))["ticket_id"]
    s1 = pv.sweep(conn, slippage_fn=lambda u: 0.0, fill_fraction_fn=lambda leg: 0.5, stamp=False)
    assert s1["partial"] == 2 and s1["filled"] == 0 and s1["tickets_filled"] == []
    assert oms.ticket_status(conn, tid) == oms.PARTIAL
    assert all(l["qty_filled"] == 65 for l in oms.ticket_view(conn, tid)["legs"])
    s2 = pv.sweep(conn, slippage_fn=lambda u: 0.0, stamp=False)
    assert s2["filled"] == 2 and oms.ticket_status(conn, tid) == oms.FILLED


def test_a_leg_without_a_limit_is_rejected_by_name_and_taints_the_ticket():
    conn = _conn()
    p = _proposal()
    p["spread"]["legs"][1]["premium"] = None
    tid = sr.issue(conn, p)["ticket_id"]
    s = pv.sweep(conn, slippage_fn=lambda u: 0.0, stamp=False)
    assert s["filled"] == 1 and s["rejected"] == 1
    v = oms.ticket_view(conn, tid)
    assert v["status"] == oms.REJECTED
    assert any("no usable limit price" in (l["reject_reason"] or "") for l in v["legs"])


def test_an_orphan_leg_is_rejected_not_crashed():
    conn = _conn()
    tid = sr.issue(conn, _proposal())["ticket_id"]
    conn.execute("DELETE FROM trade_tickets WHERE ticket_id = ?", (tid,))
    conn.commit()
    s = pv.sweep(conn, slippage_fn=lambda u: 0.0, stamp=False)
    assert s["rejected"] == 2 and s["legs"] == 0                    # both legs orphaned


def test_stamp_writes_venue_prices_onto_the_journal_legs_only_when_filled():
    conn = _conn()
    p = _proposal()
    tid = sr.issue(conn, p, journal_ref="sp000001")["ticket_id"]

    class FakeJournal:
        def __init__(self):
            self.entry = {"short_id": "sp000001", "spread": json.loads(json.dumps(p["spread"]))}
            self.updated = 0

        def update_entry(self, sid, fn):
            assert sid == "sp000001"
            self.updated += 1
            return self.entry if fn(self.entry) is not False else None
    fj = FakeJournal()
    assert pv.stamp_journal(tid, conn, journal_mod=fj) is False      # nothing filled yet
    s = pv.sweep(conn, slippage_fn=lambda u: 0.001, journal_mod=fj)
    assert s["stamped"] == 1 and fj.updated == 1
    legs = fj.entry["spread"]["legs"]
    assert legs[0]["premium"] == 100.1 and legs[0]["venue_fill"] == 100.0 and legs[0]["fill_basis"] == "venue"
    assert legs[1]["premium"] == 29.97
    assert fj.entry["spread"]["ticket_id"] == tid and fj.entry["spread"]["venue"] == "paper_venue"


def test_sweep_is_muzzled_under_pytest_without_a_connection():
    assert pv.sweep()["skip"] == "muzzled_under_pytest"


# ------------------------------------------------------------ the live path

def _wire(monkeypatch, tmp_path, flag: bool):
    from src import options_proposer as op, journal, brain_map
    monkeypatch.setattr("src.config.PAPER_VENUE_ENABLED", flag)
    conn = brain_map.connect(":memory:")
    monkeypatch.setattr(brain_map, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(op, "_PAPER_VENUE_KEEP_CONN", True)   # shared in-memory conn survives
    oms.ensure_schema(conn)
    entries = [{"short_id": "sp000001", "date": "2026-09-19", "action": "SPREAD",
                "ticker": "NIFTY 50", "shares": 65, "price": 70.0, "signal": "t",
                "decision": "pending_approval", "why": "", "pattern_tags": ["bull_call_spread"],
                "plan": None, "outcome": None, "spread": _proposal()["spread"]}]
    written = {}
    monkeypatch.setattr(journal, "read_all", lambda: entries)
    monkeypatch.setattr(journal, "rewrite_all", lambda rows: written.setdefault("rows", rows))
    monkeypatch.setattr(op, "_notify_discord", lambda *a, **k: None)
    from src import portfolio_manager as pm
    monkeypatch.setattr(pm, "gate_headless_entry", lambda *a, **k: (True, None))
    monkeypatch.setattr(pm, "release_entry", lambda *a, **k: {})
    from src.execution import paper_venue
    monkeypatch.setattr(paper_venue, "_tier_frac", lambda u, slippage_fn=None: 0.001)
    return op, conn, written


def test_an_approved_entry_is_issued_and_filled_through_the_venue_when_the_flag_is_on(monkeypatch, tmp_path):
    op, conn, written = _wire(monkeypatch, tmp_path, True)
    verdict = op.decide_pending("sp000001", approve=True, why="test", human=True)
    assert verdict["status"] == "approved"
    row = written["rows"][0]
    ex = row["execution"]
    assert ex["mode"] == "paper_venue" and ex["status"] == oms.FILLED and ex["filled_legs"] == 2
    assert ex["error"] is None
    legs = row["spread"]["legs"]
    assert legs[0]["fill_basis"] == "venue" and legs[0]["premium"] == 100.1 and legs[0]["venue_fill"] == 100.0
    assert legs[1]["premium"] == 29.97
    assert row["spread"]["ticket_id"] == ex["ticket_id"]
    assert oms.ticket_view(conn, ex["ticket_id"])["journal_ref"] == "sp000001"
    assert conn.execute("SELECT COUNT(*) FROM leg_events").fetchone()[0] == 4


def test_the_flag_off_keeps_the_legacy_instant_fill_byte_for_byte(monkeypatch, tmp_path):
    op, conn, written = _wire(monkeypatch, tmp_path, False)
    before = json.dumps(_proposal()["spread"]["legs"], sort_keys=True)
    verdict = op.decide_pending("sp000001", approve=True, why="test", human=True)
    assert verdict["status"] == "approved"
    row = written["rows"][0]
    assert row["execution"] == {"mode": "legacy_instant", "ticket_id": None, "status": None,
                                "filled_legs": 0, "error": None}
    assert json.dumps(row["spread"]["legs"], sort_keys=True) == before
    assert conn.execute("SELECT COUNT(*) FROM trade_tickets").fetchone()[0] == 0


def test_a_venue_error_fails_open_to_the_legacy_fill_and_names_it(monkeypatch, tmp_path):
    op, conn, written = _wire(monkeypatch, tmp_path, True)
    from src import strategy_router
    monkeypatch.setattr(strategy_router, "issue", lambda *a, **k: 1 / 0)
    verdict = op.decide_pending("sp000001", approve=True, why="test", human=True)
    assert verdict["status"] == "approved"
    ex = written["rows"][0]["execution"]
    assert ex["mode"] == "legacy_instant" and "ZeroDivisionError" in ex["error"]
    assert written["rows"][0]["spread"]["legs"][0]["fill_basis"] == "quoted"


def test_rejections_never_touch_the_venue(monkeypatch, tmp_path):
    op, conn, written = _wire(monkeypatch, tmp_path, True)
    verdict = op.decide_pending("sp000001", approve=False, why="no", human=True)
    assert verdict["status"] == "rejected"
    assert "execution" not in written["rows"][0]
    assert conn.execute("SELECT COUNT(*) FROM trade_tickets").fetchone()[0] == 0


def test_tracker_exit_costs_skip_entry_slippage_on_a_venue_fill():
    """The venue already paid the entry slippage; the tracker's exit math
    must not charge it twice (the #70 'quoted' rule, extended)."""
    from src import plan_tracker as pt
    spread = _proposal()["spread"]
    for leg in spread["legs"]:
        leg["fill_basis"] = "venue"
    f_v, s_v = pt._spread_exit_costs(spread, 25000.0, 0.5)
    for leg in spread["legs"]:
        leg["fill_basis"] = "ltp"
    f_l, s_l = pt._spread_exit_costs(spread, 25000.0, 0.5)
    assert f_v == f_l and s_v < s_l


# ------------------------------------------------------------ tier-1 ids

def test_tier1_fo_names_join_the_scrip_master_id_universe(tmp_path):
    from src.ingestion import scrip_master as sm
    fo = tmp_path / "fo.json"
    fo.write_text(json.dumps({"banned": ["BANNEDCO"],
                              "symbols": {"AMBER": {"tier": "tier1"}, "GAIL": {"tier": "tier1"},
                                          "BANNEDCO": {"tier": "tier1"}, "X": {"tier": "tier2"}}}))
    assert sm._tier1_fo_symbols(fo) == {"AMBER", "GAIL"}
    assert sm._tier1_fo_symbols(tmp_path / "missing.json") == set()


def test_the_venue_package_places_nothing():
    for name in ("src/execution/paper_venue.py", "src/execution/__init__.py"):
        src = (ROOT / name).read_text()
        for forbidden in ("dhanhq", "place_order", "get_option_chain", "get_live_price",
                          "firm_treasury", "portfolio_manager", "request_entry", "requests.", "httpx"):
            assert forbidden not in src, (name, forbidden)
