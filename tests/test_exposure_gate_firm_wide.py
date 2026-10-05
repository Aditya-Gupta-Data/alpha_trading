"""
Tests for Architect ruling 2 (2026-10-05), audit finding F09: decision #68's
one-position-per-underlying+direction slot is FIRM-WIDE. A position held by
ANY paper account fills it — the primary journal's open spreads as before,
plus every OPEN/EXITING PAPER_2L_LIVE row and every ACTIVE shadow lock on an
approved trade — so a LIVE position that outlives its primary (or a shadow
lock whose release failed) can no longer let a second same-direction trade
stack on one thesis.

Hermetic: ':memory:' databases or the per-test tmp brain_map.db that
tests/conftest.py points DEFAULT_DB_PATH at, tmp journal and block ledger,
chains injected, no network, no Discord.

Run:
    pytest tests/test_exposure_gate_firm_wide.py -q
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from src import brain_map, journal, oms
from src import exposure_gate as eg
from src import options_proposer as op
from src import plan_tracker as pt
from src import portfolio_manager as pm
from src import strategy_router as sr
from src.execution import live_pricer as lp
from src.execution import paper_venue as pv
from tests.test_exposure_gate import _TempLedger, _open_entry, _proposal

PRIMARY = pm.ACCOUNT_PAPER_10L
TWO_L = pm.ACCOUNT_PAPER_2L
ROT = pm.ACCOUNT_PAPER_2L_ROT
LIVE = pm.ACCOUNT_PAPER_2L_LIVE
REF = "24f931bb"
T = "NIFTY 50"


# --------------------------------------------------------------- fixtures

def _db():
    """A fresh in-memory firm: every account table + the live arm's."""
    c = brain_map.connect(":memory:")
    pm.ensure_accounts_schema(c)
    lp.ensure_schema(c)
    return c


def _resolved(ref=REF, **kw):
    """The primary's row AFTER it settled (approved, outcome stamped)."""
    return _open_entry(short_id=ref, outcome={"resolution": "ratchet_hit",
                                              "pnl_rs": 1200.0}, **kw)


def _open_live(c, ref=REF, ticker=T, strategy="bear_put_spread",
               direction="bearish"):
    """PAPER_2L_LIVE holds `ref`: its lock, then its position row, both
    through the real doors (paper_request_entry, live_pricer.open_position)."""
    entry = _open_entry(short_id=ref, ticker=ticker, strategy=strategy,
                        direction=direction)
    assert pm.paper_request_entry(c, LIVE, ref, 16764.0)["approved"]
    view = {"lots": 1, "legs": [
        {"side": "BUY", "option_type": "PE", "strike": 24050.0,
         "avg_fill_price": 208.75},
        {"side": "SELL", "option_type": "PE", "strike": 23850.0,
         "avg_fill_price": 132.0}]}
    lp.open_position(c, LIVE, entry, view, now=datetime(2026, 7, 9, 10, 0))
    return entry


def _ledger():
    return [json.loads(l) for l in eg.LEDGER_PATH.read_text().splitlines()]


# ------------------------------------- (b) a LIVE position outliving its primary

def test_live_position_that_outlives_its_primary_blocks_and_names_live():
    c = _db()
    _open_live(c)
    with _TempLedger():
        allowed, reason = eg.gate_entry(_proposal(), entries=[_resolved()],
                                        conn=c)
        rec = _ledger()[-1]
    assert allowed is False
    assert "decision #68" in reason and "firm-wide" in reason
    assert f"`{REF}` {LIVE}" in reason
    assert PRIMARY not in reason          # the primary no longer holds it
    assert rec["blocked_by"] == [REF]
    assert rec["held_by"] == {REF: [LIVE]}


def test_an_exiting_live_row_still_holds_the_slot():
    """`exiting` = the exit ticket is out but not settled: still held.
    No journal row is passed, so only the LIVE row itself (not its lock,
    which needs the journal to classify) can be what blocks."""
    c = _db()
    _open_live(c)
    c.execute("UPDATE paper_live_positions SET state = ? WHERE journal_ref = ?",
              (lp.STATE_EXITING, REF))
    c.commit()
    with _TempLedger():
        allowed, reason = eg.gate_entry(_proposal(), entries=[], conn=c)
    assert allowed is False and f"`{REF}` {LIVE}" in reason


def test_live_row_blocks_even_with_no_journal_row_at_all():
    """The LIVE row carries its own ticker/direction — it needs no journal."""
    c = _db()
    _open_live(c)
    with _TempLedger():
        allowed, reason = eg.gate_entry(_proposal(), entries=[], conn=c)
    assert allowed is False and f"`{REF}` {LIVE}" in reason


def test_opposite_direction_and_other_underlying_stay_free():
    c = _db()
    _open_live(c)                                   # bearish NIFTY 50
    with _TempLedger():
        bullish, _ = eg.gate_entry(
            _proposal(strategy="bull_call_spread", direction="bullish",
                      view="bullish"), entries=[_resolved()], conn=c)
        neutral, _ = eg.gate_entry(
            _proposal(strategy="iron_condor", direction="neutral",
                      view="neutral"), entries=[_resolved()], conn=c)
        other, _ = eg.gate_entry(_proposal(ticker="NIFTY BANK"),
                                 entries=[_resolved()], conn=c)
    assert (bullish, neutral, other) == (True, True, True)


def test_a_closed_live_row_does_not_block():
    """LIVE exits on its own quotes: _settle closes the row AND releases
    its lock in one commit — the slot is free again."""
    c = _db()
    _open_live(c)
    row = lp.open_rows(c, LIVE)[0]
    assert lp._settle(c, row, 150.0, "ratchet_hit", lp.FILL_BASIS, 0.0,
                      datetime(2026, 7, 10, 11, 0))["status"] == "settled"
    assert not lp.has_open_position(c, LIVE, REF)
    with _TempLedger():
        allowed, reason = eg.gate_entry(_proposal(), entries=[_resolved()],
                                        conn=c)
    assert (allowed, reason) == (True, "allowed")


# -------------------------------------------- (c) an orphaned active shadow lock

def test_an_orphaned_active_shadow_lock_blocks_and_names_its_account():
    """The primary settled, PAPER_2L's release failed (margin_release_error):
    its lock is still active until reconcile_orphan_locks retries it."""
    c = _db()
    assert pm.paper_request_entry(c, TWO_L, REF, 16764.0)["approved"]
    with _TempLedger():
        allowed, reason = eg.gate_entry(_proposal(), entries=[_resolved()],
                                        conn=c)
        rec = _ledger()[-1]
    assert allowed is False and f"`{REF}` {TWO_L}" in reason
    assert rec["held_by"] == {REF: [TWO_L]}
    # once the retry releases it, the slot frees
    pm.paper_release_margin(c, TWO_L, REF, 0.0)
    with _TempLedger():
        allowed, _ = eg.gate_entry(_proposal(), entries=[_resolved()], conn=c)
    assert allowed is True


def test_shadow_locks_that_are_not_positions_never_block():
    """A lock is a position only once its trade was APPROVED: a pending
    entry's proposal-time lock is a reservation (pending rows have never
    counted), a rejected entry opened nothing, and a ref with no journal
    row (an eqd: lock, a lost line) cannot be classified."""
    c = _db()
    for acct, ref in ((TWO_L, "pend0001"), (ROT, "rejd0002"),
                      (LIVE, "gone0003"), (TWO_L, "eqd:darling-7")):
        assert pm.paper_request_entry(c, acct, ref, 16764.0)["approved"]
    entries = [_open_entry(short_id="pend0001", decision="pending_approval"),
               _open_entry(short_id="rejd0002", decision="rejected")]
    with _TempLedger():
        allowed, reason = eg.gate_entry(_proposal(), entries=entries, conn=c)
    assert (allowed, reason) == (True, "allowed")


def test_an_orphan_lock_on_another_ticker_or_direction_does_not_block():
    c = _db()
    assert pm.paper_request_entry(c, TWO_L, "bank0001", 9000.0)["approved"]
    assert pm.paper_request_entry(c, ROT, "bull0002", 9000.0)["approved"]
    entries = [_resolved(ref="bank0001", ticker="NIFTY BANK"),
               _resolved(ref="bull0002", strategy="bull_call_spread",
                         direction="bullish")]
    with _TempLedger():
        allowed, _ = eg.gate_entry(_proposal(), entries=entries, conn=c)
    assert allowed is True


# ------------------------------------------- one position, several holders

def test_a_ref_held_by_the_primary_and_every_shadow_is_one_position():
    c = _db()
    for acct in (TWO_L, ROT):
        assert pm.paper_request_entry(c, acct, "ab12cd34", 16764.0)["approved"]
    _open_live(c, ref="ab12cd34")
    with _TempLedger():
        allowed, reason = eg.gate_entry(_proposal(), entries=[_open_entry()],
                                        conn=c)
        rec = _ledger()[-1]
    assert allowed is False
    assert reason.startswith("exposure gate: 1 open bearish position(s)")
    assert f"`ab12cd34` {PRIMARY}, {TWO_L}, {ROT}, {LIVE}" in reason
    assert rec["held_by"] == {"ab12cd34": [PRIMARY, TWO_L, ROT, LIVE]}


def test_primary_and_a_stacked_live_ref_are_two_positions():
    c = _db()
    _open_live(c)                                   # REF, primary settled
    entries = [_resolved(), _open_entry(short_id="ab12cd34")]
    with _TempLedger():
        allowed, reason = eg.gate_entry(_proposal(), entries=entries, conn=c)
    assert allowed is False
    assert "2 open bearish position(s)" in reason
    assert f"`ab12cd34` {PRIMARY}; `{REF}` {LIVE}" in reason


def test_discord_note_names_the_holder():
    c = _db()
    _open_live(c)
    notes = []
    with _TempLedger():
        eg.gate_entry(_proposal(), entries=[_resolved()], conn=c,
                      notify_fn=notes.append)
    assert len(notes) == 1
    assert f"`{REF}` held by {LIVE}" in notes[0]
    assert "firm-wide" in notes[0]


def test_opportunity_cost_is_hosted_only_by_a_trade_the_primary_holds():
    """The row inherits its host's outcome: a primary row that resolved
    before the block answers a different window, so a LIVE-only block
    records nothing (the no-ghost rule); a primary-held block still does."""
    c = _db()
    _open_live(c)
    calls = []
    with _TempLedger():
        eg.gate_entry(_proposal(), entries=[_resolved()], conn=c,
                      record_fn=lambda **k: calls.append(k))
        assert calls == []
        eg.gate_entry(_proposal(), entries=[_resolved(),
                                            _open_entry(short_id="ab12cd34")],
                      conn=c, record_fn=lambda **k: calls.append(k))
    assert [k["host_ref"] for k in calls] == ["ab12cd34"]


# --------------------------------------------- primary-only stays unchanged

@pytest.mark.parametrize("firm", ["empty_db", "no_db_file", "no_tables"])
def test_primary_only_behaviour_is_unchanged(firm, tmp_path, monkeypatch):
    """With nothing held outside the primary — an empty firm, no
    brain_map.db at all, or a DB without the shadow tables — every #68
    verdict is exactly the journal-only one."""
    path = tmp_path / "firm.db"
    monkeypatch.setattr(brain_map, "DEFAULT_DB_PATH", path)
    if firm != "no_db_file":
        c = brain_map.connect(path)
        if firm == "empty_db":
            pm.ensure_accounts_schema(c)
            lp.ensure_schema(c)
        c.close()
    book = [_open_entry()]
    with _TempLedger():
        blocked = eg.gate_entry(_proposal(), entries=book)
        other_dir = eg.gate_entry(
            _proposal(strategy="bull_call_spread", direction="bullish",
                      view="bullish"), entries=book)
        other_ticker = eg.gate_entry(_proposal(ticker="NIFTY BANK"),
                                     entries=book)
        nothing_open = eg.gate_entry(_proposal(), entries=[])
        rec = _ledger()[-1]
    assert blocked[0] is False
    assert "`ab12cd34` PAPER_10L" in blocked[1] and "decision #68" in blocked[1]
    assert rec["blocked_by"] == ["ab12cd34"]
    assert rec["held_by"] == {"ab12cd34": [PRIMARY]}
    assert "firm_view_unavailable" not in rec
    assert other_dir == (True, "allowed")
    assert other_ticker == (True, "allowed")
    assert nothing_open == (True, "allowed")


# ------------------------------------------------- the default (production) read

def test_default_read_is_the_real_db_path_read_only(tmp_path, monkeypatch):
    """No conn injected (run_headless's call): the gate opens
    brain_map.DEFAULT_DB_PATH `mode=ro`, sees LIVE's position — and never
    creates a table there (no ensure_schema side effect)."""
    path = tmp_path / "firm.db"
    monkeypatch.setattr(brain_map, "DEFAULT_DB_PATH", path)
    c = brain_map.connect(path)
    pm.ensure_accounts_schema(c)
    lp.ensure_schema(c)
    _open_live(c)
    c.close()
    with _TempLedger():
        allowed, reason = eg.gate_entry(_proposal(), entries=[_resolved()])
    assert allowed is False and LIVE in reason

    bare = tmp_path / "bare.db"
    brain_map.connect(bare).close()
    monkeypatch.setattr(brain_map, "DEFAULT_DB_PATH", bare)
    with _TempLedger():
        assert eg.gate_entry(_proposal(), entries=[_resolved()])[0] is True
    raw = sqlite3.connect(bare)
    names = {r[0] for r in raw.execute("SELECT name FROM sqlite_master")}
    raw.close()
    assert not names & {"paper_live_positions", "paper_margin_locks"}


# ---------------------------------------------------------- failure semantics

class _BrokenConn:
    """A database read that fails (locked past the timeout, corrupt)."""

    def execute(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")


def test_unreadable_firm_view_fails_open_but_keeps_the_primary_conflicts(capsys):
    """Same contract as an unreadable journal: the unreadable source adds
    no conflict and never blocks on its own — the primary journal's own
    conflicts still count, and the note names what was missed."""
    with _TempLedger():
        allowed, reason = eg.gate_entry(_proposal(), entries=[],
                                        conn=_BrokenConn())
        assert allowed is True
        assert "firm-wide view unavailable" in reason
        assert "database is locked" in reason
        allowed, reason = eg.gate_entry(_proposal(), entries=[_open_entry()],
                                        conn=_BrokenConn())
        rec = _ledger()[-1]
    assert allowed is False and "`ab12cd34` PAPER_10L" in reason
    assert "database is locked" in rec["firm_view_unavailable"]
    assert "firm-wide view unavailable" in capsys.readouterr().out


def test_unreadable_journal_still_fails_the_whole_gate_open(monkeypatch):
    """Unchanged: the journal is the gate's own book; unreadable = open."""
    monkeypatch.setattr(journal, "read_all",
                        lambda: (_ for _ in ()).throw(ValueError("bad line")))
    c = _db()
    _open_live(c)
    allowed, reason = eg.gate_entry(_proposal(), conn=c)
    assert allowed is True and "exposure gate unavailable" in reason


# ------------------------------- end to end: the real proposal door (F09 repro)

OPEN = datetime(2026, 9, 30, 10, 58)
MID = "NIFTY MID SELECT"


class _KeepOpen:
    """The shared file connection, immune to the callee's close()."""

    def __init__(self, c):
        object.__setattr__(self, "_c", c)

    def __getattr__(self, n):
        return getattr(self._c, n)

    def __setattr__(self, n, v):
        setattr(self._c, n, v)

    def close(self):
        pass


def _chain(quotes):
    oc = {}
    for (k, t), (bid, ask, ltp) in quotes.items():
        oc.setdefault(f"{float(k):.6f}", {})[t.lower()] = {
            "top_bid_price": bid, "top_ask_price": ask, "last_price": ltp}
    return {"last_price": 13700.0, "oc": oc}


ENTRY_CHAIN = _chain({(13725, "PE"): (207.0, 208.75, 208.0),
                      (13625, "PE"): (169.10, 170.5, 170.0)})


def _mid_spread(lots=3, basis="quoted"):
    return {"strategy": "bear_put_spread", "direction": "bearish",
            "expiry": "2026-10-27", "lot_size": 120, "lots": lots,
            "net_debit": 40.08, "net_credit": None, "entry_spot": 13731.65,
            "spread_width": 100.0, "max_profit": 7236.0, "max_loss": 4764.0,
            "reward_risk": 1.52, "margin": {"total_margin": 16764.0},
            "legs": [{"side": "BUY", "option_type": "PE", "strike": 13725.0,
                      "premium": 209.11, "fill_basis": basis},
                     {"side": "SELL", "option_type": "PE", "strike": 13625.0,
                      "premium": 169.03, "fill_basis": basis}]}


def _mid_row():
    return {"short_id": REF, "date": "2026-09-30",
            "created_at": "2026-09-30T10:58:05+05:30", "ticker": MID,
            "action": "SPREAD", "decision": "approved", "why": "t",
            "signal": "t", "price": 40.08, "spread": _mid_spread(basis="venue"),
            "ratchet": {"peak_capture_pct": 100.0, "locked_pct": 70.0,
                        "armed": True, "as_of": "2026-10-01",
                        "rungs": [{"as_of": "2026-10-01",
                                   "peak_capture_pct": 96.87,
                                   "locked_pct": 70.0}]},
            "accounts": {ROT: {"status": "approved", "lots": 1,
                               "margin_rs": 16764.0, "ticket_id": "tkt:rot"},
                         LIVE: {"status": "approved", "lots": 1,
                                "margin_rs": 16764.0, "ticket_id": "tkt:live"}},
            "outcome": None}


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A FILE database at brain_map.DEFAULT_DB_PATH: the gate's default
    read opens it `mode=ro` on its own, exactly as in production, while
    every writer shares one connection."""
    raw = brain_map.connect(brain_map.DEFAULT_DB_PATH)
    c = _KeepOpen(raw)
    pm.ensure_accounts_schema(c)
    oms.ensure_schema(c)
    lp.ensure_schema(c)
    pm.get_account(c)
    monkeypatch.setattr(brain_map, "connect", lambda *a, **k: c)
    monkeypatch.setattr(pt, "_brain_connect", lambda *a, **k: c)
    monkeypatch.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", True)
    monkeypatch.setattr("src.config.PAPER_VENUE_ENABLED", True)
    monkeypatch.setattr("src.config.RATCHET_ENABLED", True)
    monkeypatch.setattr(pv, "_tier_frac", lambda u, slippage_fn=None: 0.001)
    monkeypatch.setattr(lp, "_market_open", lambda now: True)
    monkeypatch.setattr(lp, "_now", lambda: OPEN)
    monkeypatch.setattr(pt, "_settle_spread_cash", lambda pnl: True)
    monkeypatch.setattr(eg, "LEDGER_PATH", tmp_path / "exposure_blocks.jsonl")
    hosts = []
    monkeypatch.setattr(eg, "_record_opportunity_cost",
                        lambda t, d, conflicts, **k: hosts.append(
                            [x.get("trade_id") for x in conflicts]))
    monkeypatch.setenv("PAPER_AUTO_APPROVE", "1")
    monkeypatch.setattr(op, "_PAPER_VENUE_KEEP_CONN", True)
    monkeypatch.setattr(op, "_LIVE_CHAIN_FN", lambda t, x: ENTRY_CHAIN)
    monkeypatch.setattr(op, "_memory_context_for", lambda *a, **k: "")
    monkeypatch.setattr(op, "_skeptic_note_for", lambda *a, **k: "")
    monkeypatch.setattr(op, "_format_proposal_alert",
                        lambda p, action_note="": "alert")
    monkeypatch.setattr(op, "_notify_discord", lambda *a, **k: None)
    monkeypatch.setattr("src.notifier.fire_broadcast", lambda *a, **k: None)
    monkeypatch.setattr("src.confluence.evidence.capture_for_entry",
                        lambda *a, **k: None)
    from src import human_pulse
    monkeypatch.setattr(human_pulse, "auto_approve_tripped",
                        lambda *a, **k: False)
    monkeypatch.setattr(op, "build_proposal", lambda underlying, **kw: {
        "proposal": {"ticker": MID, "shares": 360, "price": 40.08,
                     "spread": _mid_spread(), "lots": 3, "vix": 13.0,
                     "view": "bearish", "signal": "bearish trend read",
                     "action": "SPREAD"},
        "reason": "ok", "view": "bearish"})
    lp.reset_cache()
    yield c, hosts
    lp.reset_cache()
    raw.close()


def _book_mid(c):
    """24f931bb as it stood on 10-05: primary 3 lots, ROT and LIVE 1 lot
    each, LIVE's position opened through the real venue."""
    pm.request_entry(c, REF, 3 * 16764.0)
    pm.paper_request_entry(c, ROT, REF, 16764.0, lots=1, primary_lots=3)
    pm.paper_request_entry(c, LIVE, REF, 16764.0, lots=1, primary_lots=3)
    e = {"short_id": REF, "ticker": MID, "spread": _mid_spread(), "signal": "t"}
    rq = lp.requote_entry(e, now=OPEN, chain_fn=lambda t, x: ENTRY_CHAIN)
    assert rq["ok"], rq
    issued = sr.issue(c, {"ticker": MID, "short_id": REF, "signal": "t",
                          "spread": dict(e["spread"], legs=rq["legs"])},
                      journal_ref=REF, source="t", account_id=LIVE, lots=1)
    pv.sweep(c, stamp=False)
    view = oms.ticket_view(c, issued["ticket_id"])
    assert view["status"] == oms.FILLED
    lp.open_position(c, LIVE, e, view, quote_ts=rq["quote_ts"], now=OPEN)
    journal.log(_mid_row())


BARS = [("2026-09-30", 13700.0, 13760.0, 13731.65),
        ("2026-10-01", 13630.0, 13740.0, 13640.0),
        ("2026-10-05", 13660.0, 13700.0, 13680.0)]


def test_run_headless_live_outliving_its_primary_blocks_until_live_exits(
        world, monkeypatch):
    """F09 through the REAL proposal door. Before ruling 2 the second
    signal below was proposed, auto-approved, and LIVE opened a SECOND
    bearish NIFTY MID SELECT position (2 x Rs.16,764 locked)."""
    c, hosts = world
    _book_mid(c)

    # 1. the primary still holds 24f931bb: blocked, every holder named
    r1 = op.run_headless(MID, state={})
    assert r1["proposed"] is False and "decision #68" in r1["reason"], r1
    assert f"`{REF}` {PRIMARY}, {ROT}, {LIVE}" in r1["reason"]
    assert hosts == [[REF]]

    # 2. the primary settles through the real settlement code (ratchet_hit
    #    on the model); ROT settles with it, LIVE keeps position + lock
    seen = {}
    journal.update_matching(lambda e: journal.row_key(e) == REF,
                            lambda e: pt._settle_spread_row(e, BARS, seen))
    assert seen["status"] == "resolved", seen
    assert lp.has_open_position(c, LIVE, REF)
    assert pm._active_shadow_lock(c, LIVE, REF) == (16764.0, 1)

    # 3. the next same-direction signal: BLOCKED now, naming PAPER_2L_LIVE
    monkeypatch.setattr(lp, "_now", lambda: datetime(2026, 10, 6, 10, 0))
    r2 = op.run_headless(MID, state={})
    assert r2["proposed"] is False, r2
    assert f"`{REF}` {LIVE}" in r2["reason"] and PRIMARY not in r2["reason"]
    assert hosts == [[REF], []]        # no host: the primary row resolved
    assert [r["journal_ref"] for r in lp.open_rows(c, LIVE)] == [REF]
    assert pm.paper_locked_margin(c, LIVE) == 16764.0
    assert [e["short_id"] for e in journal.read_all()] == [REF]
    assert json.loads(eg.LEDGER_PATH.read_text().splitlines()[-1])[
        "held_by"] == {REF: [LIVE]}

    # 4. LIVE exits on its own quotes -> the slot is free, the firm trades
    row = lp.open_rows(c, LIVE)[0]
    assert lp._settle(c, row, 60.0, "ratchet_hit", lp.FILL_BASIS, 0.0,
                      datetime(2026, 10, 6, 11, 0))["status"] == "settled"
    r3 = op.run_headless(MID, state={})
    assert r3["proposed"] is True and r3.get("auto_approved") is True, r3
    new_ref = r3["entry"]["short_id"]
    assert [r["journal_ref"] for r in lp.open_rows(c, LIVE)] == [new_ref]
