"""
Phase M1.B — THE DUAL PAPER TREASURY (decision #102, 2026-09-19).

The Rs.2,00,000 stress-test account judged beside the Rs.10L primary on every
signal: its own tables, its own sizing, its own margin gate and halts, its
own venue tickets, settled off the primary's one exit with P&L scaled by lot
ratio. Hermetic: sqlite ':memory:', journal/venue/Discord seams patched.
"""
import json
import sqlite3
from pathlib import Path

import pytest

from src import brain_map, oms, portfolio_manager as pm, strategy_router as sr
from src.execution import paper_venue as pv
from src.reporting import markdown_ledger as ml
from src.strategy import StrategyConstructor

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def conn():
    c = brain_map.connect(":memory:")
    pm.ensure_accounts_schema(c)
    pm.get_account(c)
    yield c
    c.close()


def _spread(lots=1, max_loss=5000.0, margin=20000.0):
    sc = StrategyConstructor(vix=13.0, lot_size=65)
    s = sc.construct_bull_call_spread(24000, 24200, 100.0, 30.0)
    s["lots"] = lots
    s["max_loss"] = max_loss
    s["margin"] = dict(s.get("margin") or {}, total_margin=margin)
    s["expiry"] = "2026-10-28"
    return s


def _proposal(lots=1, **kw):
    return {"ticker": "NIFTY 50", "short_id": "sp0001", "signal": "t",
            "spread": _spread(lots=lots, **kw), "lots": lots, "vix": 13.0}


# ------------------------------------------------------------- isolation

def test_the_2l_account_seeds_at_its_pool_and_the_10l_ledger_is_untouched(conn):
    before = (pm.account_summary(conn), conn.execute("SELECT COUNT(*) FROM margin_locks").fetchone()[0])
    s = pm.paper_account_summary(conn, pm.ACCOUNT_PAPER_2L)
    assert s["account_id"] == "PAPER_2L" and s["starting_capital"] == 200000.0
    assert s["equity"] == 200000.0 and s["available_cash"] == 200000.0 and s["open_locks"] == 0
    assert (pm.account_summary(conn), conn.execute("SELECT COUNT(*) FROM margin_locks").fetchone()[0]) == before
    assert conn.execute("SELECT COUNT(*) FROM account_events").fetchone()[0] == 0


def test_a_shadow_lock_lives_only_in_the_shadow_tables(conn):
    v = pm.paper_request_entry(conn, pm.ACCOUNT_PAPER_2L, "sp0001", 20000.0, lots=1, primary_lots=3)
    assert v["approved"]
    assert conn.execute("SELECT COUNT(*) FROM margin_locks").fetchone()[0] == 0
    assert conn.execute("SELECT lots, primary_lots FROM paper_margin_locks WHERE journal_ref='sp0001'").fetchone()[:2] == (1, 3)
    assert pm.available_cash(conn) == 1_000_000.0          # primary untouched
    assert pm.paper_available_cash(conn, pm.ACCOUNT_PAPER_2L) == 180000.0
    # idempotent re-request
    assert pm.paper_request_entry(conn, pm.ACCOUNT_PAPER_2L, "sp0001", 20000.0)["reason"] == "margin already locked for this entry"
    assert conn.execute("SELECT COUNT(*) FROM paper_margin_locks").fetchone()[0] == 1


def test_unknown_accounts_are_refused_never_invented(conn):
    with pytest.raises(KeyError):
        pm.get_paper_account(conn, "PAPER_50L")


def test_primary_helpers_delegate_to_the_live_functions(conn):
    pm.request_entry(conn, "x1", 1000.0)
    assert pm.paper_locked_margin(conn, pm.ACCOUNT_PAPER_10L) == pm.locked_margin(conn) == 1000.0
    assert pm.paper_account_summary(conn, pm.ACCOUNT_PAPER_10L)["account_id"] == "PAPER_10L"


# ------------------------------------------------------------- independent judgement

def test_approved_for_10l_but_refused_for_2l_on_margin_is_logged_per_account(conn):
    # 10 lots x Rs.20k margin = Rs.2L needed; the 2L pool can afford at most 9
    # by margin and only 2 by the Rs.10k hard cap on a Rs.5k/lot max loss
    p = _proposal(lots=10)
    assert pm.request_entry(conn, "sp0001", pm.required_margin_for(p))["approved"]
    out = pm.evaluate_shadow_accounts("sp0001", p, conn=conn)
    v = out["PAPER_2L"]
    assert v["status"] == "approved" and v["lots"] == 2 and v["margin_rs"] == 40000.0
    # a second, bigger trade: 2L cash is now 1.6L, this one needs 8 lots of 30k
    p2 = _proposal(lots=8, margin=30000.0, max_loss=1000.0)
    p2["short_id"] = "sp0002"
    assert pm.request_entry(conn, "sp0002", pm.required_margin_for(p2))["approved"]   # 10L fine
    out2 = pm.evaluate_shadow_accounts("sp0002", p2, conn=conn)
    # by_risk = 200000*10%/1000 = 20, by_cap = 10, by_margin = 160000//30000 = 5 -> 5 lots, 150k margin ok
    assert out2["PAPER_2L"]["status"] == "approved" and out2["PAPER_2L"]["lots"] == 5
    # third: Rs.10k liquid left — sizing refuses a 25k/lot structure by name
    # (the same order as strategy.size_lots: cash is checked per lot first)
    p3 = _proposal(lots=1, margin=25000.0, max_loss=1000.0)
    p3["short_id"] = "sp0003"
    out3 = pm.evaluate_shadow_accounts("sp0003", p3, conn=conn)
    assert out3["PAPER_2L"]["status"] == "rejected"
    assert "sizing refused: SPAN margin Rs.25,000/lot exceeds liquid cash Rs.10,000" == out3["PAPER_2L"]["reason"]
    # and the gate itself refuses margin the cash cannot cover — in the
    # SHADOW events only, never the primary's account_events
    v = pm.paper_request_entry(conn, pm.ACCOUNT_PAPER_2L, "sp0004", 10000.01)
    assert not v["approved"] and "margin exhaustion" in v["reason"]
    ev = conn.execute("SELECT account_id, event_type, journal_ref FROM paper_account_events "
                      "WHERE event_type IN ('margin_exhaustion', 'sizing_refused') ORDER BY rowid").fetchall()
    assert [tuple(r) for r in ev] == [("PAPER_2L", "sizing_refused", "sp0003"),
                                      ("PAPER_2L", "margin_exhaustion", "sp0004")]
    assert conn.execute("SELECT COUNT(*) FROM account_events WHERE event_type='margin_exhaustion'").fetchone()[0] == 0
    assert pm.paper_account_summary(conn, pm.ACCOUNT_PAPER_2L)["rejections"] == 2


def test_sizing_never_exceeds_the_primary_lots_and_names_each_refusal(conn):
    s = _spread(lots=1, max_loss=500.0, margin=1000.0)
    assert pm.size_for_account(conn, pm.ACCOUNT_PAPER_2L, s, primary_lots=1)["lots"] == 1
    assert pm.size_for_account(conn, pm.ACCOUNT_PAPER_2L, s, primary_lots=3)["lots"] == 3
    big = _spread(lots=1, max_loss=12000.0, margin=1000.0)
    r = pm.size_for_account(conn, pm.ACCOUNT_PAPER_2L, big, primary_lots=1)
    assert r["lots"] == 0 and "hard per-trade risk cap" in r["reason"]
    thin = _spread(lots=1, max_loss=9000.0, margin=250000.0)
    r = pm.size_for_account(conn, pm.ACCOUNT_PAPER_2L, thin, primary_lots=1)
    assert r["lots"] == 0 and "exceeds liquid cash" in r["reason"]
    out = pm.evaluate_shadow_accounts("sp0009", {"spread": thin, "lots": 1}, conn=conn)
    assert out["PAPER_2L"]["status"] == "rejected" and out["PAPER_2L"]["reason"].startswith("sizing refused")
    assert conn.execute("SELECT event_type FROM paper_account_events WHERE journal_ref='sp0009'").fetchone()[0] == "sizing_refused"


def test_the_switch_off_writes_nothing(conn, monkeypatch):
    monkeypatch.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", False)
    assert pm.evaluate_shadow_accounts("sp0001", _proposal(), conn=conn) == {}
    assert conn.execute("SELECT COUNT(*) FROM paper_accounts").fetchone()[0] == 0


# ------------------------------------------------------------- one settlement path

def test_release_entry_settles_the_shadow_with_pnl_scaled_by_lot_ratio(conn, monkeypatch):
    monkeypatch.setattr(brain_map, "connect", lambda *a, **k: conn)
    p = _proposal(lots=4, margin=20000.0, max_loss=2500.0)
    pm.request_entry(conn, "sp0001", pm.required_margin_for(p))
    out = pm.evaluate_shadow_accounts("sp0001", p, conn=conn)
    assert out["PAPER_2L"]["lots"] == 4                       # cap 10k/2.5k = 4, cash fine
    # shrink the shadow to 2 lots to prove the ratio (hand-edit the lock, test only)
    conn.execute("UPDATE paper_margin_locks SET lots = 2 WHERE journal_ref = 'sp0001'")
    conn.commit()
    res = pm.release_entry("sp0001", 8000.0, conn=conn)       # primary made Rs.8,000 on 4 lots
    assert res["released"] and res["shadow_accounts"]["PAPER_2L"]["released"]
    assert res["shadow_accounts"]["PAPER_2L"]["pnl_net"] == 4000.0
    assert pm.equity(conn) == 1_008_000.0
    assert pm.paper_equity(conn, pm.ACCOUNT_PAPER_2L) == 204000.0
    assert pm.paper_locked_margin(conn, pm.ACCOUNT_PAPER_2L) == 0.0
    assert conn.execute("SELECT COUNT(*) FROM paper_equity_curve").fetchone()[0] == 1
    # a second release is a no-op on both
    res2 = pm.release_entry("sp0001", 1.0, conn=conn)
    assert not res2["released"] and "shadow_accounts" not in res2


def test_a_human_rejection_releases_the_shadow_at_zero(conn, monkeypatch):
    monkeypatch.setattr(brain_map, "connect", lambda *a, **k: conn)
    p = _proposal(lots=1)
    pm.request_entry(conn, "sp0001", pm.required_margin_for(p))
    pm.evaluate_shadow_accounts("sp0001", p, conn=conn)
    res = pm.release_entry("sp0001", 0.0, conn=conn)
    assert res["shadow_accounts"]["PAPER_2L"]["pnl_net"] == 0.0
    assert pm.paper_equity(conn, pm.ACCOUNT_PAPER_2L) == 200000.0


def test_the_shadow_has_its_own_ruin_halt_that_never_touches_the_primary(conn):
    pm.paper_request_entry(conn, pm.ACCOUNT_PAPER_2L, "L1", 10000.0)
    pm.paper_release_margin(conn, pm.ACCOUNT_PAPER_2L, "L1", -25000.0)   # -12.5%
    assert pm.paper_trading_halted(conn, pm.ACCOUNT_PAPER_2L)
    assert not pm.trading_halted(conn)
    v = pm.paper_request_entry(conn, pm.ACCOUNT_PAPER_2L, "L2", 100.0)
    assert not v["approved"] and "risk-of-ruin" in v["reason"]
    assert conn.execute("SELECT COUNT(*) FROM account_events").fetchone()[0] == 0
    # latched: the primary's clear door does not clear it, and it persists
    assert pm.paper_halt_latched(conn, pm.ACCOUNT_PAPER_2L)


# ------------------------------------------------------------- the venue

def test_venue_fills_both_accounts_tickets_and_journals_each_to_its_own_ledger(conn):
    p = _proposal(lots=3)
    prim = sr.issue(conn, p, journal_ref="sp0001", source="t")
    shad = sr.issue(conn, p, journal_ref="sp0001", source="t", account_id="PAPER_2L", lots=1)
    assert prim["ticket_id"] != shad["ticket_id"]
    assert oms.ticket_view(conn, shad["ticket_id"])["account_id"] == "PAPER_2L"
    assert oms.ticket_view(conn, shad["ticket_id"])["lots"] == 1
    assert oms.ticket_view(conn, prim["ticket_id"])["account_id"] == "PAPER_10L"
    assert oms.ticket_view(conn, shad["ticket_id"])["legs"][0]["qty_target"] == 65

    class J:
        stamped = []
        def update_entry(self, sid, fn):
            self.stamped.append(sid); return {}
    j = J()
    summary = pv.sweep(conn, slippage_fn=lambda u: 0.001, journal_mod=j)
    assert summary["by_account"] == {"PAPER_10L": 1, "PAPER_2L": 1}
    assert summary["stamped"] == 1 and j.stamped == ["sp0001"]          # journal = primary only
    assert summary["shadow_journaled"] == 1
    row = conn.execute("SELECT account_id, journal_ref, detail FROM paper_account_events "
                       "WHERE event_type='venue_fill'").fetchone()
    assert row[0] == "PAPER_2L" and row[1] == "sp0001"
    detail = json.loads(row[2])
    assert detail["lots"] == 1 and detail["legs"][0]["fill"] == 100.1 and detail["legs"][1]["fill"] == 29.97
    # the shadow ticket never stamps the journal, even when asked directly
    assert pv.stamp_journal(shad["ticket_id"], conn, journal_mod=j) is False


def test_decide_pending_issues_one_ticket_per_accepting_account(monkeypatch):
    from src import options_proposer as op, journal
    monkeypatch.setattr("src.config.PAPER_VENUE_ENABLED", True)
    c = brain_map.connect(":memory:")
    monkeypatch.setattr(brain_map, "connect", lambda *a, **k: c)
    monkeypatch.setattr(op, "_PAPER_VENUE_KEEP_CONN", True)
    oms.ensure_schema(c)
    pm.get_account(c)
    entries = [{"short_id": "sp0001", "date": "2026-09-19", "action": "SPREAD", "ticker": "NIFTY 50",
                "shares": 195, "price": 70.0, "signal": "t", "decision": "pending_approval",
                "why": "", "plan": None, "outcome": None, "spread": _spread(lots=3),
                "receipt": {"vix": 13.0}}]
    written = {}
    monkeypatch.setattr(journal, "read_all", lambda: entries)
    monkeypatch.setattr(journal, "rewrite_all", lambda rows: written.setdefault("rows", rows))
    monkeypatch.setattr(op, "_notify_discord", lambda *a, **k: None)
    monkeypatch.setattr(pv, "_tier_frac", lambda u, slippage_fn=None: 0.001)
    monkeypatch.setattr(pm, "gate_headless_entry",            # the real gate, on the shared conn
                        lambda ref, m, conn=None: (pm.request_entry(c, ref, m)["approved"], ""))
    verdict = op.decide_pending("sp0001", approve=True, why="t", human=True)
    assert verdict["status"] == "approved"
    row = written["rows"][0]
    acc = row["accounts"]["PAPER_2L"]
    assert acc["status"] == "approved" and acc["lots"] == 2          # 10k cap / 5k max loss
    assert acc["ticket_id"] and acc["venue_status"] == oms.FILLED
    assert row["execution"]["accounts"]["PAPER_2L"]["lots"] == 2
    assert c.execute("SELECT COUNT(*) FROM trade_tickets").fetchone()[0] == 2
    assert c.execute("SELECT lots FROM trade_tickets WHERE account_id='PAPER_2L'").fetchone()[0] == 2
    assert c.execute("SELECT lots FROM trade_tickets WHERE account_id='PAPER_10L'").fetchone()[0] == 3
    assert pm.paper_locked_margin(c, pm.ACCOUNT_PAPER_2L) == 40000.0
    assert c.execute("SELECT COUNT(*) FROM margin_locks WHERE released_at IS NULL").fetchone()[0] == 1
    # the primary ticket alone stamps the journal legs
    assert row["spread"]["ticket_id"] == row["execution"]["ticket_id"]
    c.close()


def test_decide_pending_records_a_shadow_refusal_and_issues_no_shadow_ticket(monkeypatch):
    from src import options_proposer as op, journal
    monkeypatch.setattr("src.config.PAPER_VENUE_ENABLED", True)
    c = brain_map.connect(":memory:")
    monkeypatch.setattr(brain_map, "connect", lambda *a, **k: c)
    monkeypatch.setattr(op, "_PAPER_VENUE_KEEP_CONN", True)
    oms.ensure_schema(c)
    pm.get_account(c)
    pm.paper_request_entry(c, pm.ACCOUNT_PAPER_2L, "older", 195000.0)   # 2L nearly full
    entries = [{"short_id": "sp0001", "date": "2026-09-19", "action": "SPREAD", "ticker": "NIFTY 50",
                "shares": 65, "price": 70.0, "signal": "t", "decision": "pending_approval",
                "why": "", "plan": None, "outcome": None, "spread": _spread(lots=1),
                "receipt": {"vix": 13.0}}]
    written = {}
    monkeypatch.setattr(journal, "read_all", lambda: entries)
    monkeypatch.setattr(journal, "rewrite_all", lambda rows: written.setdefault("rows", rows))
    monkeypatch.setattr(op, "_notify_discord", lambda *a, **k: None)
    monkeypatch.setattr(pv, "_tier_frac", lambda u, slippage_fn=None: 0.001)
    monkeypatch.setattr(pm, "gate_headless_entry",            # the real gate, on the shared conn
                        lambda ref, m, conn=None: (pm.request_entry(c, ref, m)["approved"], ""))
    verdict = op.decide_pending("sp0001", approve=True, why="t", human=True)
    assert verdict["status"] == "approved"
    row = written["rows"][0]
    assert row["accounts"]["PAPER_2L"]["status"] == "rejected"
    assert "exceeds liquid cash Rs.5,000" in row["accounts"]["PAPER_2L"]["reason"]
    assert row["execution"]["mode"] == "paper_venue" and "accounts" not in row["execution"]
    assert c.execute("SELECT COUNT(*) FROM trade_tickets").fetchone()[0] == 1
    c.close()


# ------------------------------------------------------------- the book

def test_the_book_shows_both_accounts_side_by_side(tmp_path):
    db = tmp_path / "brain_map.db"
    c = sqlite3.connect(db)
    pm.ensure_accounts_schema(c)
    c.execute("INSERT INTO account_state VALUES (1, 1000000, 95901.49, 1095901.49, '2026-07-21T09:00:00')")
    c.execute("INSERT INTO paper_accounts VALUES ('PAPER_2L', 200000, -3200.5, 200000, '2026-09-19T09:00:00')")
    c.execute("INSERT INTO paper_margin_locks VALUES ('PAPER_2L', 'sp1', 40000, 2, 3, '2026-09-19T09:17:00', NULL, NULL)")
    c.execute("INSERT INTO paper_equity_curve VALUES ('PAPER_2L', '2026-09-19T15:30:00', 196799.5, 200000, 1.6)")
    c.execute("INSERT INTO paper_account_events VALUES ('PAPER_2L', '2026-09-19T10:00:00', 'margin_exhaustion', 'sp2', 'x')")
    c.commit(); c.close()
    text = ml.render(journal_path=tmp_path / "j.jsonl", equity_path=tmp_path / "e.jsonl",
                     db_path=db, today="2026-09-19", now="test")
    assert "## Accounts — side by side (paper)" in text
    assert "| Metric | PAPER_10L (primary) | PAPER_2L (shadow) |" in text
    assert "| Starting capital | ₹1,000,000 | ₹200,000 |" in text
    assert "| Equity | ₹1,095,901 | ₹196,800 |" in text
    assert "| Margin locked (open) | ₹0 | ₹40,000 |" in text
    assert "| Available margin | ₹1,095,901 | ₹156,800 |" in text
    assert "| Open trades | 0 | 1 |" in text
    assert "| Drawdown now | — | -1.60% |" in text
    assert "| Refusals (this account only) | — | 1 |" in text


def test_the_book_without_shadow_tables_renders_the_primary_alone(tmp_path):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    pm.ensure_schema(c)
    c.execute("INSERT INTO account_state VALUES (1, 1000000, 0, 1000000, '2026-07-21T09:00:00')")
    c.commit(); c.close()
    text = ml.render(journal_path=tmp_path / "j.jsonl", equity_path=tmp_path / "e.jsonl",
                     db_path=db, today="2026-09-19", now="test")
    assert "side by side" not in text and "| Equity | ₹1,000,000 |" in text


# ------------------------------------------------------------- house rules

def test_no_order_path_and_the_journal_stays_the_primary_book():
    for name in ("src/portfolio_manager.py", "src/execution/paper_venue.py", "src/oms.py",
                 "src/strategy_router.py"):
        src = (ROOT / name).read_text()
        for forbidden in ("dhanhq", "place_order", "get_live_price", "get_option_chain"):
            assert forbidden not in src, (name, forbidden)
    venue = (ROOT / "src/execution/paper_venue.py").read_text()
    assert "journal.jsonl" in venue and "portfolio_manager" not in venue
    op = (ROOT / "src/options_proposer.py").read_text()
    assert op.count("strategy_router.issue(") == 1
