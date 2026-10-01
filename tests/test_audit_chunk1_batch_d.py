"""
Audit Chunk 1, Batch D and the two leftovers (decision #123, 2026-10-01).

  D9   portfolio.json: one lock for every read-modify-write, atomic saves
  D10  the dashboard mirror publishes consistent snapshots, never a live file
  D11  equity desk: ledger BEFORE lock; a lock with no ledger entry is swept
  D12  same-second equity-curve points resolve by write order (rowid)
  D14  recon's book drops ticket-only rows the journal says are settled
  leftovers: eviction quotes fetched outside the journal lock; an eviction
  whose own loss would trip the daily breaker / ruin halt is refused
Hermetic: tmp paths (conftest), ':memory:' or tmp sqlite, injected quotes.
"""
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src import brain_map, journal, oms, portfolio as pf, portfolio_manager as pm
from src import knowledge_graph_logger as kg
from src.execution import recon_engine as recon

IST = pm.IST


@pytest.fixture(autouse=True)
def _tmp_portfolio(monkeypatch, tmp_path):
    monkeypatch.setattr(pf, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pf, "PORTFOLIO_PATH", tmp_path / "portfolio.json")


# ===================================================================== D9

def test_concurrent_credits_are_never_lost():
    pf.save({"cash": 1000.0, "holdings": {}})

    def credit():
        for _ in range(40):
            pf.update(lambda b: b.update(cash=round(b["cash"] + 1.0, 2)))
    threads = [threading.Thread(target=credit) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert pf.load()["cash"] == 1120.0
    assert not [p for p in pf.PORTFOLIO_PATH.parent.iterdir() if p.name.endswith(".tmp")]


def test_update_aborts_cleanly_and_the_lock_is_reentrant():
    pf.save({"cash": 10.0, "holdings": {}})
    before = pf.PORTFOLIO_PATH.read_text()
    assert pf.update(lambda b: False) is False
    assert pf.PORTFOLIO_PATH.read_text() == before
    with pf.locked():
        pf.update(lambda b: b.update(cash=11.0))          # nested: no deadlock
    assert pf.load()["cash"] == 11.0


def test_every_paper_cash_writer_goes_through_the_lock(monkeypatch):
    """The tracker's credit and position close used to load, then save the
    copy they loaded; both now run inside pf.update (fresh read, one lock)."""
    from src import plan_tracker as pt
    pf.save({"cash": 100.0, "holdings": {"TCS.NS": {"shares": 2, "avg_price": 10.0}}})
    held = []
    real_update = pf.update
    monkeypatch.setattr(pf, "update", lambda fn: held.append(1) or real_update(fn))
    assert pt._settle_spread_cash(25.0) is True
    assert pt._close_paper_position({"ticker": "TCS.NS"}, 12.0) is True
    assert pt._close_paper_position({"ticker": "INFY.NS"}, 12.0) is False
    assert held == [1, 1, 1]
    book = pf.load()
    assert "TCS.NS" not in book["holdings"] and book["cash"] > 125.0


# ==================================================================== D10

def test_the_mirror_snapshot_is_consistent_while_a_writer_is_mid_commit(tmp_path, monkeypatch):
    from src.dashboard import mirror_snapshot as ms
    db = tmp_path / "live.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (x INTEGER)")
    c.execute("INSERT INTO t VALUES (1)")
    c.commit()
    c.execute("INSERT INTO t VALUES (2)")                 # an open, uncommitted write
    assert c.in_transaction
    out = tmp_path / "snap"
    out.mkdir()
    assert ms.snapshot_db(db, out / "brain_map.db") is True
    s = sqlite3.connect(out / "brain_map.db")
    assert s.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert [r[0] for r in s.execute("SELECT x FROM t")] == [1]        # committed state only
    s.close()
    c.rollback()
    c.close()
    journal.log({"short_id": "m1"})
    assert ms.snapshot_journal(out / "journal.jsonl") is True
    assert json.loads((out / "journal.jsonl").read_text().splitlines()[0])["short_id"] == "m1"


def test_the_publish_script_never_pushes_the_live_db():
    text = (Path(__file__).resolve().parents[1] / "scripts" / "publish_dashboard_mirror.sh").read_text()
    loop = [l for l in text.splitlines() if l.startswith("for f in") and "data/brain_map.db" in l]
    assert loop == [] and "src.dashboard.mirror_snapshot" in text


# ==================================================================== D11

def _desk_world(tmp_path):
    import src.equity_desk as desk
    import src.firm_treasury as ft
    desk.EQUITY_DESK_ENABLED = True
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pm.get_account(conn)
    ft.get_budget(conn)
    return desk, conn


def _entry(sym="TCS"):
    import src.equity_shadow_proposer as sp
    row = {"symbol": sym, "valuation": 35, "forensic": 64, "close": 2269.0,
           "buy_zone": [2189.72, 2293.28], "stop": 2085.0, "extension": "normal",
           "tier": "strong_buy", "family": "buy", "in_zone": True, "pinned": None,
           "rule": "in zone"}
    return sp.evaluate_darling_entry(row, {"symbol": sym, "status": "ok",
                                           "trim_levels": [2460.0, 2510.0],
                                           "anchored_vwap": 2240.0}, "2026-07-21")


def _run_entries(tmp_path, commit_fn, capital_fn, ledger):
    import src.equity_shadow_proposer as sp
    tiers = tmp_path / "tiers.json"
    levels = tmp_path / "levels.json"
    row = {"symbol": "TCS", "valuation": 35, "forensic": 64, "close": 2269.0,
           "buy_zone": [2189.72, 2293.28], "stop": 2085.0, "extension": "normal",
           "tier": "strong_buy", "family": "buy", "in_zone": True, "pinned": None,
           "rule": "in zone"}
    tiers.write_text(json.dumps({"as_of": datetime.now(IST).isoformat(), "tiers": {"strong_buy": [row]}}))
    levels.write_text(json.dumps({"levels": [{"symbol": "TCS", "status": "ok",
                                              "trim_levels": [2460.0, 2510.0], "anchored_vwap": 2240.0}]}))
    return sp.propose_darling_entries(tiers_path=tiers, levels_path=levels, path=ledger,
                                      check_fn=lambda p: {"allowed": True}, universe={},
                                      capital_fn=capital_fn, commit_fn=commit_fn,
                                      quote_fn=lambda t: 2250.0, fill_basis="live",
                                      nifty_trend_fn=lambda: None)


def test_the_entry_is_on_the_ledger_before_its_lock_is_taken(tmp_path):
    desk, conn = _desk_world(tmp_path)
    ledger = tmp_path / "shadow.jsonl"
    seen = {}

    def commit(e):
        seen["on_ledger"] = any(x.get("id") == e.get("id") and x.get("event") == "entry"
                                for x in kg.read_events(ledger))
        seen["locked_before"] = conn.execute("SELECT COUNT(*) FROM margin_locks").fetchone()[0]
        return desk.commit_funding(e, conn=conn)
    [row] = _run_entries(tmp_path, commit, lambda e: desk.fund_entry(e, conn=conn, commit=False), ledger)
    assert seen == {"on_ledger": True, "locked_before": 0}
    assert row["funding"]["funded"] is True
    assert desk.desk_state(conn)["deployed"] == row["funding"]["notional"]


def test_an_entry_that_never_reached_the_ledger_locks_nothing(tmp_path, monkeypatch):
    desk, conn = _desk_world(tmp_path)
    ledger = tmp_path / "shadow.jsonl"
    monkeypatch.setattr(kg, "log_event", lambda ev, path=None: dict(ev, _persisted=False))
    calls = []
    [row] = _run_entries(tmp_path, lambda e: calls.append(e) or {"funded": True},
                         lambda e: desk.fund_entry(e, conn=conn, commit=False), ledger)
    assert calls == [] and row["funding"]["funded"] is False
    assert conn.execute("SELECT COUNT(*) FROM margin_locks").fetchone()[0] == 0


def test_a_lock_refused_after_logging_is_named_on_the_ledger(tmp_path):
    desk, conn = _desk_world(tmp_path)
    ledger = tmp_path / "shadow.jsonl"
    [row] = _run_entries(tmp_path, lambda e: {"funded": False, "reason": "cash taken"},
                         lambda e: desk.fund_entry(e, conn=conn, commit=False), ledger)
    assert row["funding"]["funded"] is False and "cash taken" in row["funding"]["reason"]
    events = kg.read_events(ledger)
    assert [e["event"] for e in events] == ["entry", "funding_revoked"]
    assert events[1]["id"] == events[0]["id"]
    assert conn.execute("SELECT COUNT(*) FROM margin_locks").fetchone()[0] == 0


def test_an_eqd_lock_with_no_ledger_entry_is_swept_after_the_grace(tmp_path):
    desk, conn = _desk_world(tmp_path)
    ledger = tmp_path / "shadow.jsonl"
    ledger.write_text("")
    now = datetime(2026, 10, 1, 12, 0, tzinfo=IST)
    for ref, minutes in (("eqd:old00001", 90), ("eqd:new00001", 5)):
        assert pm.request_entry(conn, ref, 1000.0)["approved"]
        conn.execute("UPDATE margin_locks SET locked_at = ? WHERE journal_ref = ?",
                     ((now - timedelta(minutes=minutes)).replace(tzinfo=None).isoformat(), ref))
    conn.commit()
    out = desk.sweep_orphan_locks(ledger_path=ledger, conn=conn, now=now)
    assert [s["lock_ref"] for s in out] == ["eqd:old00001"]
    active = {r[0] for r in conn.execute("SELECT journal_ref FROM margin_locks WHERE released_at IS NULL")}
    assert active == {"eqd:new00001"}
    assert conn.execute("SELECT COUNT(*) FROM account_events WHERE event_type = "
                        "'equity_orphan_lock_released'").fetchone()[0] == 1


# ==================================================================== D12

def test_same_second_curve_points_resolve_by_write_order(tmp_path):
    from src import eod_summary
    from src.reporting import markdown_ledger as ml
    db = tmp_path / "bm.db"
    c = brain_map.connect(db)
    pm.ensure_accounts_schema(c)
    pm.get_account(c)
    for eq in (1_000_000.0, 1_000_014.49):                  # the 09-24 shape: same second
        c.execute("INSERT INTO equity_curve (ts, equity, peak_equity, drawdown_pct) "
                  "VALUES ('2026-09-24T09:21:59', ?, ?, 0)", (eq, eq))
    c.commit()
    c.close()
    assert "1,000,014" in (eod_summary.drawdown_line(db_path=db) or "")
    assert ml.read_account(db_path=db)["curve"]["equity"] == 1_000_014.49


# ==================================================================== D14

def test_recon_drops_ticket_only_rows_the_journal_has_settled(tmp_path):
    c = sqlite3.connect(tmp_path / "bm.db")
    pm.ensure_accounts_schema(c)
    c.execute("CREATE TABLE trade_tickets (ticket_id TEXT, journal_ref TEXT, underlying TEXT, "
              "status TEXT, kind TEXT, account_id TEXT)")
    c.execute("CREATE TABLE paper_live_positions (account_id TEXT, journal_ref TEXT, state TEXT)")
    for tid, ref, acct in (("t1", "open0001", "PAPER_10L"), ("t2", "setl0001", "PAPER_10L"),
                           ("t3", "rejd0001", "PAPER_10L"), ("t4", "live0001", "PAPER_2L_LIVE"),
                           ("t5", "eqd:x1", "PAPER_10L"), ("t6", "eqd:x2", "PAPER_10L")):
        c.execute("INSERT INTO trade_tickets VALUES (?, ?, 'NIFTY', 'FILLED', 'ENTRY', ?)", (tid, ref, acct))
    c.execute("INSERT INTO paper_live_positions VALUES ('PAPER_2L_LIVE', 'live0001', 'closed')")
    c.commit()
    pm.request_entry(c, "eqd:x2", 1000.0)                   # the desk still holds x2
    rows = [{"short_id": "open0001", "decision": "approved", "outcome": None},
            {"short_id": "setl0001", "decision": "approved", "outcome": {"resolution": "expiry_backstop"}},
            {"short_id": "rejd0001", "decision": "rejected", "outcome": None},
            {"short_id": "live0001", "decision": "approved", "outcome": None}]
    book = recon.read_paper_book(conn=c, journal_rows=rows)
    keys = {(r["ref"], r["account"]) for r in book}
    # open0001 stays (open, ticket only); eqd:x2 stays (a lock holds it);
    # settled / rejected / live-closed / lock-less non-journal refs go
    assert keys == {("open0001", "PAPER_10L"), ("eqd:x2", "PAPER_10L")}
    c.close()


# ============================================================== leftovers

def test_an_eviction_that_would_trip_the_daily_breaker_is_refused(monkeypatch):
    from src import plan_tracker as pt
    import src.execution.paper_venue as pv
    from tests.test_capital_rotation import QUOTES, _entry as rot_entry
    ROT = pm.ACCOUNT_PAPER_2L_ROT
    monkeypatch.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", True)
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", False)
    monkeypatch.setattr(pv, "_tier_frac", lambda u, slippage_fn=None: 0.001)
    c = brain_map.connect(":memory:")
    pm.ensure_accounts_schema(c)
    oms.ensure_schema(c)
    pm.get_paper_account(c, ROT)
    pm.paper_request_entry(c, ROT, "old1", 20000.0, lots=1, primary_lots=1)
    # already down 2.95% of the session today: Rs.5,900 realized on a closed trade
    pm.paper_request_entry(c, ROT, "lost", 1000.0)
    pm.paper_release_margin(c, ROT, "lost", -5900.0)
    rows = [rot_entry("old1")]
    losing = {(24000.0, "CE"): 60.0, (24200.0, "CE"): 30.0}    # 30 vs a 70 debit: -40/share
    res = pt.evict_for_rotation(c, ROT, "old1", 1, max_rr_left=99.0, quotes_fn=lambda e: losing,
                                entries=rows, need_rs=1.0)
    assert res["status"] == "would_trip_breaker"
    assert pm._active_shadow_lock(c, ROT, "old1") is not None
    assert c.execute("SELECT COUNT(*) FROM trade_tickets").fetchone()[0] == 0
    c.close()


def test_the_eviction_quotes_are_fetched_outside_the_journal_lock(monkeypatch):
    from src import options_proposer as op, plan_tracker as pt
    import src.live_bridge as lb
    seen = []
    monkeypatch.setattr(op, "journal", journal)
    monkeypatch.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", True)
    monkeypatch.setattr(pm, "evaluate_eviction", lambda *a, **k: {"evict": True,
                                                                  "weakest": {"journal_ref": "held0001"}})
    monkeypatch.setattr(pt, "rotation_marks", lambda refs, **k: seen.append(("marks", journal.lock_held()))
                        or {r: 0.1 for r in refs})
    monkeypatch.setattr(lb, "_leg_quotes_for", lambda e: seen.append(("quotes", journal.lock_held()))
                        or {(1.0, "CE"): 1.0})
    journal.log({"short_id": "pend0001", "decision": "pending_approval", "outcome": None,
                 "spread": {"legs": [{"strike": 1}], "lot_size": 1, "max_profit": 1, "max_loss": 1,
                            "margin": {"total_margin": 1.0}}})
    journal.log({"short_id": "held0001", "decision": "approved", "outcome": None, "spread": {"legs": []}})
    c = brain_map.connect()
    pm.ensure_accounts_schema(c)
    pm.paper_request_entry(c, pm.ACCOUNT_PAPER_2L_ROT, "held0001", 100.0)
    c.close()
    got = op._prefetch_rotation("pend0001")
    assert got["marks"] == {"held0001": 0.1} and got["quotes"] == {"held0001": {(1.0, "CE"): 1.0}}
    assert seen == [("marks", False), ("quotes", False)]
    # inside the lock the eviction sees ONLY these — no network seam is called
    marks_fn, evict_fn = op._rotation_fns(got)
    assert marks_fn(["held0001", "other"]) == {"held0001": 0.1}
