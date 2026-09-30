"""
Audit Chunk 1 fixes (decisions #121 and #122, 2026-09-30).

  D1/D2  the journal: one cross-process lock for every write, atomic
         whole-file writes, and the tracker / approvals write only their own
         row onto a FRESH read (a stale copy can no longer revert anything)
  D3     a failed margin release is named and retried; shadows still settle
  D4     (tests/test_live_account.py: the live settle is one transaction)
  D5     the one-off Brain Map repair tool (src/ stays append-only)
  D6     (tests/test_capital_rotation.py: eviction only at approval)
  D7     pending margin expires at the 15:30 close; approval renews it
  D8     an audited clear-halt door for every paper account
  D13    a ratio-settled shadow account pays its own flat brokerage

Hermetic: tmp journal (conftest), ':memory:' or tmp sqlite, no network.
"""
import asyncio
import importlib.util
import json
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from src import brain_map, journal, oms, portfolio as pf, portfolio_manager as pm
from src import options_proposer as op, plan_tracker as pt
from src.strategy import StrategyConstructor

ROOT = Path(__file__).resolve().parents[1]
IST = pm.IST
TWO_L = pm.ACCOUNT_PAPER_2L


@pytest.fixture(autouse=True)
def _real_journal_and_two_accounts(monkeypatch):
    """Another test file leaves a FakeJournal on plan_tracker; these tests
    drive the REAL journal module (pointed at tmp by conftest)."""
    monkeypatch.setattr(pt, "journal", journal)
    monkeypatch.setattr(op, "journal", journal)
    monkeypatch.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", False)
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", False)


@pytest.fixture
def conn():
    c = brain_map.connect(":memory:")
    pm.ensure_accounts_schema(c)
    pm.get_account(c)
    yield c
    c.close()


def _condor():
    return StrategyConstructor(vix=13.0, lot_size=75).construct_iron_condor(
        24800, 25200, 200, 95, 38, 100, 40)


def _spread_row(sid, ticker="NIFTY 50", decision="approved", **kw):
    ic = _condor()
    row = {"short_id": sid, "date": "2026-07-06", "action": "SPREAD", "ticker": ticker,
           "price": ic["net_credit"], "shares": 75, "signal": "range", "decision": decision,
           "why": "t", "pattern_tags": [], "plan": None, "outcome": None,
           "spread": dict(ic, lots=1, expiry="2026-07-26", entry_spot=25000.0)}
    row.update(kw)
    return row


RANGE_BARS = [(f"2026-07-{d:02d}", 24950.0, 25050.0, 25000.0) for d in range(7, 26)]


def _rows():
    return {journal.row_key(e): e for e in journal.read_all()}


def _wire_tracker(monkeypatch, tmp_path, bars_fn, released=None, settled=None):
    """run_tracker on the REAL tmp journal, everything else a seam."""
    db = tmp_path / "bm.db"
    monkeypatch.setattr(pt, "_daily_bars", bars_fn)
    monkeypatch.setattr(pt, "_settle_spread_cash",
                        lambda pnl: (settled.append(pnl) if settled is not None else None) or True)
    monkeypatch.setattr(pt, "_brain_connect", lambda: brain_map.connect(db))
    monkeypatch.setattr(pt.analyst, "generate_post_mortem", lambda *a: None)
    monkeypatch.setattr(pt, "_today", lambda: date(2026, 7, 20))
    monkeypatch.setattr("src.config.PAPER_VENUE_ENABLED", False)
    monkeypatch.setattr(pm, "release_entry",
                        lambda ref, pnl=0.0, conn=None, **k:
                        (released.append((ref, pnl, k)) if released is not None else None)
                        or {"released": True})
    monkeypatch.setattr("src.notifier.fire_broadcast", lambda *a, **k: None)
    return db


# ================================================================ D1 / D2

def test_every_write_is_atomic_and_leaves_no_temp_file():
    for sid in ("a1", "a2", "a3"):
        journal.log({"short_id": sid, "outcome": None})
    assert journal.update_entry("a2", lambda e: e.update(outcome={"x": 1})) is not None
    assert [e["short_id"] for e in journal.read_all()] == ["a1", "a2", "a3"]
    assert _rows()["a2"]["outcome"] == {"x": 1}
    assert not [p for p in journal.JOURNAL_PATH.parent.iterdir() if p.name.endswith(".tmp")]
    # an aborting mutate writes nothing
    before = journal.JOURNAL_PATH.read_text()
    assert journal.update_entry("a1", lambda e: False) is None
    assert journal.JOURNAL_PATH.read_text() == before


def test_the_lock_is_reentrant_in_a_thread_and_exclusive_across_threads():
    journal.log({"short_id": "r1", "outcome": None})
    done = threading.Event()

    def other_writer():
        journal.log({"short_id": "r2", "outcome": None})
        done.set()

    with journal.locked():
        assert journal.lock_held()
        journal.update_entry("r1", lambda e: e.update(n=1))     # nested: no deadlock
        t = threading.Thread(target=other_writer)
        t.start()
        time.sleep(0.15)
        assert not done.is_set()                                 # blocked on our lock
    t.join(timeout=5)
    assert done.is_set() and set(_rows()) == {"r1", "r2"} and _rows()["r1"]["n"] == 1


def test_a_stuck_lock_times_out_instead_of_hanging():
    import fcntl
    path = journal._lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(journal.JournalLockTimeout):
            with journal.locked(timeout=0.2):
                pass
        fcntl.flock(held, fcntl.LOCK_UN)


def test_a_row_journaled_mid_sweep_survives_the_tracker(monkeypatch, tmp_path):
    """D1: the tracker used to rewrite the WHOLE file from the copy it read
    before its network work — a proposal journaled meanwhile vanished."""
    journal.log(_spread_row("sp000001"))
    calls = []

    def bars(ticker, start):
        if not calls:
            journal.log(_spread_row("new00001", decision="pending_approval"))   # the scheduler
        calls.append(ticker)
        return RANGE_BARS
    _wire_tracker(monkeypatch, tmp_path, bars)
    assert pt.run_tracker(email=False) == 1
    rows = _rows()
    assert rows["sp000001"]["outcome"]["resolution"] == "profit_take"
    assert "new00001" in rows and rows["new00001"]["outcome"] is None


def test_a_trade_settled_elsewhere_mid_sweep_is_never_settled_twice(monkeypatch, tmp_path):
    """D1: the intraday square-off settles the row while the tracker is
    fetching bars — the tracker must skip it: no second cash credit, no
    second release, and the square-off's outcome stands."""
    journal.log(_spread_row("sp000001"))
    released, settled = [], []

    def bars(ticker, start):
        journal.update_entry("sp000001", lambda e: e.update(outcome={"resolution": "profit_take",
                                                                     "pnl_rs": 111.0,
                                                                     "exit_basis": "intraday_chain"}))
        return RANGE_BARS
    _wire_tracker(monkeypatch, tmp_path, bars, released=released, settled=settled)
    assert pt.run_tracker(email=False) == 0
    assert released == [] and settled == []
    assert _rows()["sp000001"]["outcome"] == {"resolution": "profit_take", "pnl_rs": 111.0,
                                              "exit_basis": "intraday_chain"}


def test_a_ratchet_raised_mid_sweep_is_not_lowered_by_another_rows_resolution(monkeypatch, tmp_path):
    """D1: row A resolving used to rewrite row B from the stale copy,
    lowering the ratchet lock the live bridge had just raised on B."""
    journal.log(_spread_row("aaaa0001"))
    journal.log(_spread_row("bbbb0001", ticker="NIFTY BANK"))

    def bars(ticker, start):
        if ticker == "NIFTY 50":
            assert pt.note_ratchet("bbbb0001", 62.0, 30.0)
            return RANGE_BARS
        return RANGE_BARS[:2]                  # B stays live
    _wire_tracker(monkeypatch, tmp_path, bars)
    assert pt.run_tracker(email=False) == 1
    rows = _rows()
    assert rows["aaaa0001"]["outcome"] is not None
    assert rows["bbbb0001"]["outcome"] is None and rows["bbbb0001"]["ratchet"]["locked_pct"] == 30.0


def _wire_decide(monkeypatch, execute):
    monkeypatch.setattr(pm, "gate_headless_entry", lambda ref, req, conn=None: (True, "ok"))
    monkeypatch.setattr(op, "_judge_shadow_accounts", lambda *a, **k: {})
    monkeypatch.setattr(op, "_execute_paper_entry", execute)
    monkeypatch.setattr(op, "_notify_discord", lambda *a, **k: None)
    monkeypatch.setattr("src.notifier.fire_broadcast", lambda *a, **k: None)


def test_an_approval_writes_only_its_row_so_a_nested_stamp_survives(monkeypatch):
    """D1: decide_pending used to rewrite the whole file from its own copy —
    erasing the rotation stamp its own eviction had just written on the
    EVICTED trade's row."""
    journal.log(_spread_row("pend0001", decision="pending_approval"))
    journal.log(_spread_row("held0001"))

    def execute(entry, **k):
        journal.update_entry("held0001", lambda e: e.update(stamp="evicted"))   # nested write
        return {"mode": "legacy_instant", "ticket_id": None, "status": None,
                "filled_legs": 0, "error": None}
    _wire_decide(monkeypatch, execute)
    v = op.decide_pending("pend0001", approve=True, why="ok", human=False)
    assert v["status"] == "approved"
    rows = _rows()
    assert rows["pend0001"]["decision"] == "approved" and rows["held0001"]["stamp"] == "evicted"


def test_the_same_entry_cannot_be_approved_twice(monkeypatch):
    journal.log(_spread_row("pend0001", decision="pending_approval"))
    calls = []
    _wire_decide(monkeypatch, lambda e, **k: calls.append(e["short_id"]) or {"mode": "legacy_instant"})
    assert op.decide_pending("pend0001", approve=True, human=False)["status"] == "approved"
    assert op.decide_pending("pend0001", approve=True, human=False)["status"] == "not_found"
    assert calls == ["pend0001"]


# ===================================================================== D3

def test_a_failed_primary_release_still_settles_the_shadows_and_says_so(conn, monkeypatch):
    pm.request_entry(conn, "ref00001", 20000.0)
    pm.paper_request_entry(conn, TWO_L, "ref00001", 20000.0, lots=1, primary_lots=1)
    monkeypatch.setattr(pm, "release_margin",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    res = pm.release_entry("ref00001", 1000.0, conn=conn)
    assert res["released"] is False and "database is locked" in res["error"]
    assert res["shadow_accounts"][TWO_L]["released"] is True
    assert pm._active_shadow_lock(conn, TWO_L, "ref00001") is None


def _lock_age(conn, ref, minutes, now):
    stamp = (now - timedelta(minutes=minutes)).replace(tzinfo=None).isoformat(timespec="seconds")
    conn.execute("UPDATE margin_locks SET locked_at = ? WHERE journal_ref = ?", (stamp, ref))
    conn.execute("UPDATE paper_margin_locks SET locked_at = ? WHERE journal_ref = ?", (stamp, ref))
    conn.commit()


def test_reconcile_releases_only_the_locks_the_settlement_path_should_have(conn):
    now = datetime(2026, 9, 30, 12, 0, tzinfo=IST)
    settled = _spread_row("setl0001", outcome={"resolution": "profit_take", "pnl_rs": 5000.0,
                                               "frictions_rs": 300.0, "hypothetical": False})
    for row in (settled,
                _spread_row("open0001"),
                _spread_row("pend0001", decision="pending_approval"),
                _spread_row("rejd0001", decision="rejected")):
        journal.log(row)
    for ref in ("setl0001", "open0001", "pend0001", "rejd0001", "ghost001", "young001", "eqd:77"):
        assert pm.request_entry(conn, ref, 1000.0)["approved"]
    pm.paper_request_entry(conn, TWO_L, "setl0001", 1000.0, lots=1, primary_lots=2)
    _lock_age(conn, "ghost001", 120, now)
    _lock_age(conn, "young001", 10, now)
    out = pt.reconcile_orphan_locks(now=now, conn=conn)
    assert set(out) == {"setl0001", "rejd0001", "ghost001"}
    active = {r[0] for r in conn.execute("SELECT journal_ref FROM margin_locks WHERE released_at IS NULL")}
    assert active == {"open0001", "pend0001", "young001", "eqd:77"}
    pnl = dict(conn.execute("SELECT journal_ref, pnl_net FROM margin_locks WHERE released_at IS NOT NULL"))
    assert pnl == {"setl0001": 5000.0, "rejd0001": 0.0, "ghost001": 0.0}
    flat = pf.flat_order_cost_rs(8)                                   # 4 legs x (entry + exit)
    two_l = conn.execute("SELECT pnl_net FROM paper_margin_locks WHERE journal_ref = 'setl0001'").fetchone()[0]
    assert two_l == pytest.approx(0.5 * (5000.0 + flat) - flat)
    ev = [r[0] for r in conn.execute("SELECT detail FROM account_events WHERE event_type = 'orphan_lock_released'")]
    assert len(ev) == 3 and any("no journal row" in d for d in ev)
    assert pt.reconcile_orphan_locks(now=now, conn=conn) == {}         # idempotent


# ===================================================================== D7

def test_the_pending_cutoff_is_the_1530_close_of_the_proposal_day():
    assert pt.pending_cutoff({"created_at": "2026-09-30T10:00:00+05:30"}) == \
        datetime(2026, 9, 30, 15, 30, tzinfo=IST)
    # the closing cycle can journal a few seconds after 15:30:00 — same session
    assert pt.pending_cutoff({"created_at": "2026-09-30T15:30:40+05:30"}) == \
        datetime(2026, 9, 30, 15, 30, tzinfo=IST)
    assert pt.pending_cutoff({"created_at": "2026-09-30T16:05:00+05:30"}) == \
        datetime(2026, 10, 1, 15, 30, tzinfo=IST)
    assert pt.pending_cutoff({"date": "2026-09-30"}) == datetime(2026, 9, 30, 15, 30, tzinfo=IST)
    assert pt.pending_cutoff({}) is None


def test_unapproved_margin_expires_at_the_close_and_approval_renews_it(conn):
    journal.log(_spread_row("pend0001", decision="pending_approval",
                            created_at="2026-09-30T10:00:00+05:30"))
    journal.log(_spread_row("appr0001", created_at="2026-09-30T10:00:00+05:30"))
    assert pm.request_entry(conn, "pend0001", 20000.0)["approved"]
    assert pm.request_entry(conn, "appr0001", 20000.0)["approved"]
    pm.paper_request_entry(conn, TWO_L, "pend0001", 20000.0, lots=1, primary_lots=1)
    curve_before = conn.execute("SELECT COUNT(*) FROM equity_curve").fetchone()[0]
    eq_before = pm.equity(conn)

    assert pt.expire_pending_margin(now=datetime(2026, 9, 30, 15, 29, tzinfo=IST), conn=conn) == {}
    out = pt.expire_pending_margin(now=datetime(2026, 9, 30, 15, 31, tzinfo=IST), conn=conn)
    assert out == {"pend0001": {pm.ACCOUNT_PAPER_10L: 20000.0, TWO_L: 20000.0}}
    assert pm.locked_margin(conn) == 20000.0                          # appr0001 keeps its lock
    assert pm.paper_locked_margin(conn, TWO_L) == 0.0
    assert pm.equity(conn) == eq_before                               # nothing realized
    assert conn.execute("SELECT COUNT(*) FROM equity_curve").fetchone()[0] == curve_before
    assert conn.execute("SELECT COUNT(*) FROM account_events WHERE event_type = ?",
                        (pm.PENDING_LOCK_EXPIRED_EVENT,)).fetchone()[0] == 1
    assert pt.expire_pending_margin(now=datetime(2026, 9, 30, 16, 0, tzinfo=IST), conn=conn) == {}
    assert _rows()["pend0001"]["decision"] == "pending_approval"      # the proposal stays pending

    # a later approval takes the margin again, judged on that day's cash
    again = pm.request_entry(conn, "pend0001", 21000.0)
    assert again["approved"] and "renewed" in again["reason"]
    assert pm.locked_margin(conn) == 41000.0
    shadow = pm.paper_request_entry(conn, TWO_L, "pend0001", 21000.0, lots=1, primary_lots=1)
    assert shadow["approved"] and pm.paper_locked_margin(conn, TWO_L) == 21000.0


def test_a_settled_ref_is_never_locked_again(conn, monkeypatch):
    """It used to hit the PRIMARY KEY, and the gate failed OPEN."""
    pm.request_entry(conn, "done0001", 1000.0)
    pm.release_margin(conn, "done0001", 50.0)
    v = pm.request_entry(conn, "done0001", 1000.0)
    assert v["approved"] is False and "already settled" in v["reason"]
    monkeypatch.setattr(brain_map, "connect", lambda *a, **k: conn)
    allowed, _ = pm.gate_headless_entry("done0001", 1000.0, conn=conn)
    assert allowed is False


def test_the_scheduler_runs_the_1530_sweep_when_the_session_closes(monkeypatch):
    from src import master_scheduler as ms
    seen = []
    monkeypatch.setattr(pt, "expire_pending_margin", lambda now=None, conn=None: seen.append(now) or {})
    clock = {"now": datetime(2026, 7, 6, 11, 0, tzinfo=IST)}

    async def idle():
        while True:
            await asyncio.sleep(0.01)

    async def scenario():
        task = asyncio.create_task(ms.run_trading_session(
            ("NIFTY BANK",), now_fn=lambda: clock["now"], entry_loop=idle, exit_loop=idle,
            notify_fn=lambda t: None, playbook_fn=lambda u, **k: [], account_fn=lambda: [],
            poll_seconds=0.02))
        await asyncio.sleep(0.05)
        clock["now"] = datetime(2026, 7, 6, 15, 31, tzinfo=IST)
        return await asyncio.wait_for(task, timeout=2)
    assert asyncio.run(scenario())["status"] == "completed"
    assert seen == [datetime(2026, 7, 6, 15, 31, tzinfo=IST)]


# ===================================================================== D8

def test_every_paper_account_has_the_92_clear_halt_door(conn):
    pm.get_paper_account(conn, TWO_L)
    assert pm.paper_clear_halt(conn, TWO_L, why="x")["reason"] == "no latched halt to clear"
    pm.paper_log_event(conn, TWO_L, pm.HALT_LATCH_EVENT, None, "test latch")
    assert pm.paper_trading_halted(conn, TWO_L)
    refused = pm.paper_clear_halt(conn, TWO_L, why="  ")
    assert refused["cleared"] is False and "stated why" in refused["reason"]
    assert pm.paper_trading_halted(conn, TWO_L)
    out = pm.paper_clear_halt(conn, TWO_L, why="reviewed the losers", who="owner (test)")
    assert out["cleared"] and not out["halted"] and not pm.paper_trading_halted(conn, TWO_L)
    row = conn.execute("SELECT detail FROM paper_account_events WHERE account_id = ? AND event_type = ?",
                       (TWO_L, pm.HALT_CLEAR_EVENT)).fetchone()
    assert "owner (test)" in row[0] and "reviewed the losers" in row[0]
    # still breached -> the latch re-arms at once (#92)
    conn.execute("UPDATE paper_accounts SET realized_pnl = -25000 WHERE account_id = ?", (TWO_L,))
    conn.commit()
    assert pm.paper_trading_halted(conn, TWO_L)
    again = pm.paper_clear_halt(conn, TWO_L, why="try")
    assert again["cleared"] and again["halted"] and pm.paper_trading_halted(conn, TWO_L)
    # the primary delegates to clear_halt; unknown accounts are refused
    assert pm.paper_clear_halt(conn, pm.ACCOUNT_PAPER_10L, why="x")["reason"] == "no latched halt to clear"
    assert "unknown" in pm.paper_clear_halt(conn, "PAPER_9L", why="x")["reason"]


# ==================================================================== D13

def test_a_shadow_pays_its_own_flat_brokerage():
    flat = pf.flat_order_cost_rs(8)
    assert flat == pytest.approx(8 * 20.0 * 1.18)
    assert pm.shadow_pnl(8000.0, 2, 4, flat) == pytest.approx(0.5 * (8000.0 + flat) - flat)
    assert pm.shadow_pnl(8000.0, 2, 4, 0.0) == 4000.0                 # the pre-#122 figure
    assert pm.shadow_pnl(-1234.5, 3, 3, flat) == -1234.5              # same size = same P&L
    assert pm.shadow_pnl(0.0, 1, 2, 0.0) == 0.0                       # rejection / expiry


def test_the_tracker_hands_the_flat_part_to_the_shadow_release(monkeypatch, tmp_path):
    journal.log(_spread_row("sp000001"))
    released = []
    _wire_tracker(monkeypatch, tmp_path, lambda t, s: RANGE_BARS, released=released)
    assert pt.run_tracker(email=False) == 1
    (ref, pnl, kw), = released
    assert ref == "sp000001" and kw["flat_frictions_rs"] == pf.flat_order_cost_rs(8)
    assert _rows()["sp000001"]["outcome"]["pnl_rs"] == pnl


# ===================================================================== D5

def _outcome_db(path):
    c = brain_map.connect(path)
    for ref, res, r in (("54365ef1", "loss", -1.14), ("2ff3443a", "loss", -0.56),
                        ("f8356c9c", "loss", -1.0), ("bd73554d", "loss", -1.0),
                        ("efe1681e", "loss", -1.0), ("other001", "win", 0.5)):
        oid = brain_map.record_outcome(c, ref, "2026-09-23", "NIFTY 50", archetype="bear_put_spread",
                                       r_multiple=r, result=res, post_mortem={"note": "voided stop"})
        ev = brain_map.record_event(c, "2026-09-10", "NIFTY 50", "signal", "bear_put_spread")
        brain_map.link_event_outcome(c, ev, oid)
    c.close()


def _resolved(sid, pnl, r, day):
    return _spread_row(sid, spread=dict(_condor(), strategy="bear_put_spread", lots=1,
                                        expiry="2026-10-28"),
                       outcome={"resolution": "profit_take", "pnl_rs": pnl, "r_multiple": r,
                                "exit_date": day})


def test_the_repair_statements_exist_only_in_the_offline_tool(tmp_path):
    """#121 is a one-time exception: src/ stays append-only (the
    loss-permanence source guard), the replace/delete live in the tool."""
    tool = _load_tool()
    assert not hasattr(brain_map, "replace_outcome") and not hasattr(brain_map, "delete_outcome")
    assert (ROOT / "scripts" / "repair_d5_brain_map_outcomes.py").read_text().startswith(
        "# MANUAL OFFLINE TOOL")
    db = tmp_path / "o.db"
    _outcome_db(db)
    c = brain_map.connect(db)
    oid = c.execute("SELECT id FROM outcomes WHERE journal_ref = '54365ef1'").fetchone()[0]
    res = tool.replace_outcome(c, "54365ef1", {"date": "2026-09-29", "ticker": "NIFTY 50",
                                               "archetype": "bear_put_spread", "r_multiple": 0.9,
                                               "result": "win", "regime_trend": None,
                                               "regime_vix": None})
    assert res["replaced"] and res["before"]["result"] == "loss"
    row = c.execute("SELECT id, result, r_multiple, date, post_mortem FROM outcomes "
                    "WHERE journal_ref = '54365ef1'").fetchone()
    assert tuple(row) == (oid, "win", 0.9, "2026-09-29", None)          # same id: links survive
    assert c.execute("SELECT COUNT(*) FROM event_outcome_link WHERE outcome_id = ?", (oid,)).fetchone()[0] == 1
    gone = tool.delete_outcome(c, "f8356c9c")
    assert gone["deleted"] and gone["links"] == 1
    assert c.execute("SELECT COUNT(*) FROM outcomes WHERE journal_ref = 'f8356c9c'").fetchone()[0] == 0
    assert tool.replace_outcome(c, "nope", {}) == {"replaced": False, "before": None}
    c.close()


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "repair_d5", ROOT / "scripts" / "repair_d5_brain_map_outcomes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_d5_tool_syncs_the_five_rows_to_the_journal_after_a_backup(tmp_path, capsys):
    db = tmp_path / "brain_map.db"
    _outcome_db(db)
    journal.log(_resolved("54365ef1", 11136.68, 1.79, "2026-09-26"))
    journal.log(_resolved("2ff3443a", 14482.63, 2.32, "2026-09-29"))
    journal.log(_spread_row("f8356c9c"))                                   # still OPEN
    journal.log(_resolved("bd73554d", -3000.0, -0.48, "2026-09-28"))
    journal.log(_resolved("efe1681e", -2500.0, -0.40, "2026-09-28"))
    journal.log(_resolved("other001", -100.0, -0.02, "2026-09-01"))        # mismatched, NOT authorised
    tool = _load_tool()

    before = db.read_bytes()
    assert tool.main(["--db", str(db)]) == 0                             # dry run
    assert db.read_bytes() == before
    assert "DRY RUN — 5 row(s) would change" in capsys.readouterr().out

    assert tool.main(["--db", str(db), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "VERIFIED: all five match the journal." in out
    assert "other001: brain map win vs journal loss" in out
    assert len(list(tmp_path.glob("brain_map.db.bak-d5-*"))) == 1
    c = brain_map.connect(db)
    got = {r["journal_ref"]: (r["result"], r["r_multiple"], r["date"])
           for r in c.execute("SELECT * FROM outcomes")}
    assert got["54365ef1"] == ("win", 1.79, "2026-09-26")
    assert got["2ff3443a"] == ("win", 2.32, "2026-09-29")
    assert got["bd73554d"] == ("loss", -0.48, "2026-09-28")
    assert "f8356c9c" not in got
    assert got["other001"] == ("win", 0.5, "2026-09-23")                  # reported, untouched
    c.close()
    archive = [json.loads(l) for l in (tmp_path / "d5_brain_map_outcome_repairs.jsonl").read_text().splitlines()]
    assert len(archive) == 5 and {a["action"] for a in archive} == {"replace", "delete"}
    assert all(a["row_before"]["result"] == "loss" for a in archive)
    assert all(len(a["links_before"]) == 1 for a in archive)            # the links are archived too
    assert tool.main(["--db", str(db), "--yes"]) == 0                    # idempotent
    assert "Nothing to repair." in capsys.readouterr().out


def test_every_brain_map_connection_waits_30_seconds_for_a_busy_writer(monkeypatch, tmp_path):
    seen = {}
    real = sqlite3.connect
    monkeypatch.setattr(brain_map.sqlite3, "connect",
                        lambda path, **kw: seen.update(kw) or real(path, **kw))
    brain_map.connect(tmp_path / "t.db").close()
    assert seen.get("timeout") == 30.0


def test_the_wealth_sweep_runs_after_the_journal_lock_is_released(monkeypatch, tmp_path):
    """Panel finding on 4c5a838: the GOLDBEES sweep (a live quote with
    retries and a Discord post) ran inside the settlement's journal lock,
    stalling every other writer. It now runs after the row is written."""
    journal.log(_spread_row("sp000001"))
    _wire_tracker(monkeypatch, tmp_path, lambda t, s: RANGE_BARS)
    calls = []
    monkeypatch.setattr(pm, "release_entry", lambda ref, pnl=0.0, conn=None, **k:
                        calls.append(("release", k.get("wealth_sweep"), journal.lock_held()))
                        or {"released": True, "wealth_sweep_due": True})
    monkeypatch.setattr(pm, "run_wealth_sweep", lambda ref, pnl:
                        calls.append(("sweep", ref, journal.lock_held())))
    assert pt.run_tracker(email=False) == 1
    assert calls == [("release", False, True), ("sweep", "sp000001", False)]


def test_a_live_position_keeps_its_lock_through_the_pending_expiry(conn, monkeypatch):
    from src.execution import live_pricer
    live = pm.ACCOUNT_PAPER_2L_LIVE
    journal.log(_spread_row("pend0002", decision="pending_approval",
                            created_at="2026-09-30T10:00:00+05:30"))
    pm.paper_request_entry(conn, live, "pend0002", 5000.0)
    monkeypatch.setattr(live_pricer, "has_open_position", lambda c, a, r: True)
    out = pt.expire_pending_margin(now=datetime(2026, 9, 30, 15, 31, tzinfo=IST), conn=conn)
    assert out == {} and pm._active_shadow_lock(conn, live, "pend0002") is not None


def test_the_live_requote_is_fetched_before_the_journal_lock(monkeypatch):
    """#122 panel: decide_pending used to make the live arm's Dhan chain
    call while holding the journal lock every other writer waits on."""
    from src.execution import live_pricer
    journal.log(_spread_row("pend0003", decision="pending_approval"))
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "_legs_all_quoted", lambda s: True)
    seen = {}
    monkeypatch.setattr(live_pricer, "requote_entry", lambda e, **k:
                        seen.setdefault("held", journal.lock_held()) is not None
                        and {"ok": True, "legs": [], "quote_ts": "t"})

    def execute(entry, live_requote=None):
        seen["passed"] = live_requote
        return {"mode": "legacy_instant"}
    _wire_decide(monkeypatch, execute)
    assert op.decide_pending("pend0003", approve=True, human=False)["status"] == "approved"
    assert seen["held"] is False and seen["passed"]["ok"] is True


def test_the_eod_walk_persists_a_live_spreads_ratchet(monkeypatch, tmp_path):
    """Panel finding on 4c5a838: with per-row writes, the ratchet the EOD
    walk stamps on a spread that stays OPEN was never written (before, it
    rode along only when another row resolved in the same run)."""
    from src import profit_ratchet as prm
    row = _spread_row("dir00001")
    row["spread"]["strategy"] = "bull_call_spread"
    journal.log(row)
    monkeypatch.setattr(prm, "is_directional", lambda s: True)
    monkeypatch.setattr("src.config.RATCHET_EFFECTIVE_DATE", "2099-01-01")   # arm, never fire
    _wire_tracker(monkeypatch, tmp_path, lambda t, s: RANGE_BARS[:10])
    assert pt.run_tracker(email=False) == 0
    got = _rows()["dir00001"]
    assert got["outcome"] is None and got["ratchet"]["as_of"] == RANGE_BARS[9][0]


def test_a_lock_timeout_skips_one_row_not_the_whole_sweep(monkeypatch, tmp_path):
    journal.log(_spread_row("aaaa0001"))
    journal.log(_spread_row("bbbb0001", ticker="NIFTY BANK"))
    _wire_tracker(monkeypatch, tmp_path, lambda t, s: RANGE_BARS)
    real = journal.update_matching
    calls = []

    def flaky(match, mutate):
        calls.append(1)
        if len(calls) == 1:
            raise journal.JournalLockTimeout("held")
        return real(match, mutate)
    monkeypatch.setattr(journal, "update_matching", flaky)
    assert pt.run_tracker(email=False) == 1
    rows = _rows()
    assert rows["aaaa0001"]["outcome"] is None and rows["bbbb0001"]["outcome"] is not None


def _directional_row(sid, monkeypatch=None):
    s = StrategyConstructor(vix=13.0, lot_size=65).construct_bull_call_spread(24000, 24200, 100.0, 30.0)
    s.update(lots=1, expiry="2026-07-26", entry_spot=24000.0)
    return _spread_row(sid, spread=s, shares=65, price=s.get("net_debit"))


def _capture_bars(row, captures):
    """Daily closes (from 2026-07-07) whose modeled capture is as close as
    possible to each target % (a bull call's capture rises with spot)."""
    spread = row["spread"]
    lot = int(spread["lot_size"])
    mp = float(spread["max_profit"]) / lot
    ml = float(spread["max_loss"]) / lot
    entry = pt._spread_entry_mark(spread)
    expiry = date.fromisoformat(spread["expiry"])
    total = max(1, (expiry - date.fromisoformat(row["date"])).days)
    bars = []
    for i, want in enumerate(captures):
        day = date(2026, 7, 7) + timedelta(days=i)
        frac = max(0.0, (expiry - day).days / total)
        _, spot = min((abs(max(-ml, min(pt._spread_mark(spread, float(sp), frac) - entry, mp)) / mp * 100
                           - want), sp) for sp in range(23900, 24300))
        bars.append((day.isoformat(), float(spot), float(spot), float(spot)))
    return bars


def test_a_saved_ratchet_never_judges_earlier_closes(monkeypatch):
    """#122 panel round 2 (blocker, found in 37a7708): the walk seeded the
    SAVED lock at the entry day, so a lock raised later (an intraday rung, or
    the walk's own persisted state) fired a BACKDATED ratchet_hit on an older,
    weaker close. The saved state now applies from its own date forward."""
    monkeypatch.setattr("src.config.RATCHET_EFFECTIVE_DATE", "2026-07-01")
    row = _directional_row("dir00002")
    bars = _capture_bars(row, [10, 20, 35, 38])          # never armed by its own closes
    assert pt._resolve_spread(json.loads(json.dumps(row)), bars) is None
    # an intraday rung raised on the LAST bar's day (lock 30): it must not
    # judge the 10% / 20% closes before it; the 38% close on its day is above
    row["ratchet"] = {"peak_capture_pct": 62.0, "locked_pct": 30.0, "armed": True,
                      "as_of": bars[-1][0], "source": "live_bridge"}
    assert pt._resolve_spread(json.loads(json.dumps(row)), bars) is None
    # a rung raised TODAY, after the last bar: kept as saved, never lowered
    row["ratchet"]["as_of"] = "2026-12-31"
    walked = json.loads(json.dumps(row))
    assert pt._resolve_spread(walked, bars) is None
    assert walked["ratchet"]["locked_pct"] == 30.0                    # effective state keeps it
    assert walked["ratchet"]["rungs"][0]["as_of"] == "2026-12-31"      # at its own date
    # the same lock DOES judge a close on/after its own date
    row["ratchet"]["as_of"] = bars[-1][0]
    later = bars + _capture_bars(row, [10, 20, 35, 38, 12])[4:]
    hit = pt._resolve_spread(json.loads(json.dumps(row)), later)
    assert hit is not None and hit[0] == "ratchet_hit" and hit[3] == later[-1][0]


def test_the_persisted_walk_state_does_not_backdate_the_next_run(monkeypatch, tmp_path):
    monkeypatch.setattr("src.config.RATCHET_EFFECTIVE_DATE", "2026-07-01")
    row = _directional_row("dir00003")
    bars = _capture_bars(row, [5, 65, 62])              # armed at 60 -> lock 30 on day 2
    journal.log(row)
    _wire_tracker(monkeypatch, tmp_path, lambda t, s: bars)
    assert pt.run_tracker(email=False) == 0              # run 1: open, ratchet saved
    assert _rows()["dir00003"]["ratchet"]["locked_pct"] is not None
    assert pt.run_tracker(email=False) == 0              # run 2, same bars: still open
    assert _rows()["dir00003"]["outcome"] is None


def test_reconcile_still_sweeps_a_committed_release_when_a_later_ref_raises(conn, monkeypatch):
    journal.log(_spread_row("setl0001", outcome={"resolution": "profit_take", "pnl_rs": 5000.0,
                                                 "frictions_rs": 300.0, "hypothetical": False}))
    journal.log(_spread_row("setl0002", outcome={"resolution": "profit_take", "pnl_rs": 100.0,
                                                 "frictions_rs": 300.0, "hypothetical": False}))
    pm.request_entry(conn, "setl0001", 1000.0)
    pm.request_entry(conn, "setl0002", 1000.0)
    swept, logged = [], []

    def flaky_log(c, kind, detail=""):
        logged.append(detail)
        if len(logged) == 2:
            raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(pm, "log_event", flaky_log)
    monkeypatch.setattr(pm, "run_wealth_sweep", lambda ref, pnl: swept.append((ref, pnl)))
    with pytest.raises(sqlite3.OperationalError):
        pt.reconcile_orphan_locks(now=datetime(2026, 9, 30, 12, 0, tzinfo=IST), conn=conn)
    assert swept == [("setl0001", 5000.0), ("setl0002", 100.0)]



def test_each_intraday_rung_judges_from_its_own_date(monkeypatch):
    """#122 panel round 3: one saved as_of meant a later rung moved an
    earlier rung's date, so the close between them escaped the earlier lock.
    note_ratchet now keeps every rung dated; the walk folds each at its own."""
    monkeypatch.setattr("src.config.RATCHET_EFFECTIVE_DATE", "2026-07-01")
    row = _directional_row("dir00004")
    bars = _capture_bars(row, [10, 20, 25, 35])       # closes never arm by themselves
    journal.log(row)
    monkeypatch.setattr(pt, "date", type("D", (date,), {"today": staticmethod(lambda: date(2026, 7, 8))}))
    assert pt.note_ratchet("dir00004", 62.0, 30.0)                  # rung A, 07-08: lock 30
    monkeypatch.setattr(pt, "date", type("D", (date,), {"today": staticmethod(lambda: date(2026, 7, 10))}))
    assert pt.note_ratchet("dir00004", 85.0, 50.0)                  # rung B, 07-10: lock 50
    monkeypatch.setattr(pt, "date", date)            # (never monkeypatch.undo(): it reverts conftest too)
    saved = _rows()["dir00004"]
    assert [r["as_of"] for r in saved["ratchet"]["rungs"]] == ["2026-07-08", "2026-07-10"]
    hit = pt._resolve_spread(json.loads(json.dumps(saved)), bars)
    # the 07-08 close (20%) is judged by rung A's lock 30 — not rung B's 50,
    # and not left unjudged until 07-10
    assert hit is not None and hit[0] == "ratchet_hit" and hit[3] == "2026-07-08"


def test_a_newer_rung_is_merged_with_what_the_closes_built_never_replacing_it(monkeypatch):
    """#122 panel round 3: when a saved rung is newer than every bar, the
    walk used to restore it verbatim and discard a HIGHER lock the closes
    had just built. The effective state is now the max of both."""
    monkeypatch.setattr("src.config.RATCHET_EFFECTIVE_DATE", "2099-01-01")   # judge nothing
    row = _directional_row("dir00005")
    bars = _capture_bars(row, [10, 85])                # the 07-08 close arms 80 -> lock 50
    row["ratchet"] = {"peak_capture_pct": 62.0, "locked_pct": 30.0, "as_of": "2026-12-31",
                      "rungs": [{"as_of": "2026-12-31", "peak_capture_pct": 62.0, "locked_pct": 30.0}]}
    walked = json.loads(json.dumps(row))
    assert pt._resolve_spread(walked, bars) is None
    assert walked["ratchet"]["locked_pct"] == 50.0
    rungs = walked["ratchet"]["rungs"]
    assert row["ratchet"]["rungs"][0] in rungs                          # the intraday rung, dated
    assert {"as_of": bars[1][0], "locked_pct": 50.0} .items() <= next(
        r for r in rungs if r.get("source") == "close").items()        # the close-earned lock, dated


def test_a_lock_earned_on_a_close_survives_a_partial_bar_series(monkeypatch):
    """#122 panel round 4: the walk's own state was not durable — one run on
    a Dhan series with a dropped row rebuilt a LOWER lock and wrote it. A
    lock earned on a close is now kept as a dated rung like an intraday one."""
    monkeypatch.setattr("src.config.RATCHET_EFFECTIVE_DATE", "2099-01-01")   # judge nothing
    row = _directional_row("dir00006")
    bars = _capture_bars(row, [10, 65, 45])            # the 07-08 close arms 60 -> lock 30
    walked = json.loads(json.dumps(row))
    assert pt._resolve_spread(walked, bars) is None and walked["ratchet"]["locked_pct"] == 30.0
    partial = [bars[0], bars[2]]                       # the 07-08 row dropped by the feed
    again = json.loads(json.dumps(walked))
    assert pt._resolve_spread(again, partial) is None
    assert again["ratchet"]["locked_pct"] == 30.0      # not lowered
    # and still never backdated: the 07-08 rung does not judge the 07-07 close
    monkeypatch.setattr("src.config.RATCHET_EFFECTIVE_DATE", "2026-07-01")
    assert pt._resolve_spread(json.loads(json.dumps(walked)), bars) is None
