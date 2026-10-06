"""
PAPER_2L_LIVE — audit Chunk 2 lows F04 + F07 + F08 + F15 + F16 and the L1
review's residuals (2026-10-06, batch L2).

F04: a HELD exit re-fetched the option chain on every 60-s tick for as long
as the hold lasted. Now a predicate already refused on the cached snapshot,
or one inside the quote interval of the last exit attempt, is not
re-verified on a new chain: one fetch per LIVE_QUOTE_INTERVAL_SECONDS.

F07: the live_exit audit event was written AFTER the money commit; a failed
INSERT lost it for good and the settled exit was logged 'tick failed'. Now
row close + lock release + P&L + event are ONE transaction; a missed
journal stamp is re-stamped by the expiry backstop's pass.

F08: the expiry backstop skipped rows left `exiting`, holding their lock for
ever. Now it resolves them through the completion's exclusive claim: fills
settle from the fills; nothing filled settles like an open row; a partly
filled basket books its closed legs at their fills and its open legs at the
expiry session's intrinsic, unclamped. Its settle is a compare-and-set on
the state it read.

F15: a FILLED live entry whose row was never written was released at Rs.0 as
'never opened' when the primary settled first. Now the lock is kept for the
tick's one repair door.

F16: expire_pending_lock's LIVE guard ran DDL (an implicit COMMIT) inside
the function's one transaction. Now nothing is expired unless everything is.

L1 residuals: (a) the completion's claim refuses a claim in flight INSIDE the
compare-and-set; (b) a zero-bid leg floored on ANY ticket is booked at its
quote; (c) three surviving mutants pinned.

Hermetic: sqlite ':memory:' or tmp_path files, chains injected, the ticket
wall clock faked, the journal in tmp (conftest), nothing under data/ or logs/.
"""
import json
import sqlite3
from datetime import date, datetime

import pytest

from src import brain_map, journal, oms, plan_tracker as pt, portfolio_manager as pm
from src import strategy_router as sr
from src.execution import live_pricer as lp, paper_venue as pv
from tests.test_live_exit_ownership_and_fills import (  # noqa: F401  (fixtures + the L1 helpers)
    world, two, _open, _row, _exit_tickets, _exit_qty_by_leg, _chain, _cut_basket, _events, _event_details,
    _NoFill, _Raise, _condor, _bull_call, _fail_second_leg_fill, CONDOR_ENTRY, CONDOR_PX, ENTRY, LATER,
    LIVE, OPEN, PX)

REAL_STAMP = lp._stamp_journal          # captured before any fixture stubs it
TWO_L = pm.ACCOUNT_PAPER_2L

# 2026-10-27 is inside the 2-day pre-expiry window of the 10-28 expiry
DEAD = _chain({(24000, "CE"): (None, 1.0, None), (24200, "CE"): (4.0, 5.0, 4.5)})   # exit loses 75 > max 70
OK = _chain({(24000, "CE"): (90.0, 92.0, 91.0), (24200, "CE"): (24.0, 26.0, 25.0)})  # exit at 64: loses 6
UPTO_27 = [("2026-10-27", 24000.0, 24300.0, 24250.0)]
EOD = datetime(2026, 10, 29, 5, 35)


def _bars(close):
    return UPTO_27 + [("2026-10-28", 24000.0, 24400.0, close)]


def _tick_at(c, now, chain, epoch, **kw):
    fn = chain if callable(chain) else (lambda t, x: chain)
    return lp.tick(now=now, conn=c, chain_fn=fn, sleep_fn=lambda s: None, now_epoch_fn=lambda: float(epoch),
                   interval_s=300, **kw)


def _lock_active(c, ref="lx0001"):
    return pm._active_shadow_lock(c, LIVE, ref) is not None


def _realized(c):
    return pm.paper_account_summary(c, LIVE)["realized_pnl"]


# =================================================================== F04 — held exits respect the interval

def test_a_held_exit_re_verifies_once_per_quote_interval_not_every_tick(world):
    c = world
    _open(c)
    calls = []
    fn = lambda t, x: calls.append(x) or DEAD
    seen = []
    for m in range(10):                                   # ten 60-s ticks, quote interval 300 s
        t = _tick_at(c, datetime(2026, 10, 27, 11, m), fn, 5000.0 + 60 * m)
        (x,) = t["exits"]
        seen.append((x["signal"], x["status"], bool(x.get("held_on_cached_chain"))))
    assert len(calls) == 2                                # epochs 5000 and 5300 — it used to be 10
    assert [s[:2] for s in seen] == [("pre_expiry_exit", "held_loss_beyond_max")] * 10
    assert [s[2] for s in seen] == [False, True, True, True, True, False, True, True, True, True]
    assert _events(c, lp.EVENT_EXIT_HELD) == 1            # still one ledger row a day
    assert _row(c)["state"] == "open" and _exit_tickets(c) == []


def test_an_unfilled_exit_is_not_re_verified_inside_its_quote_interval(world):
    c = world
    _open(c)
    calls, statuses = [], []
    for m in range(10):
        t = _tick_at(c, datetime(2026, 10, 27, 11, m), lambda tk, x: calls.append(x) or OK, 5000.0 + 60 * m,
                     venue_mod=_NoFill)
        statuses.append(t["exits"][0]["status"])
    assert statuses == ["unfilled"] + ["held_recent_attempt"] * 4 + ["unfilled"] + ["held_recent_attempt"] * 4
    assert len(calls) == 2 and len(_exit_tickets(c)) == 2


def test_the_hold_is_re_decided_on_the_next_due_fetch(world):
    """The quotes recover: the fetch that falls due at the interval sees it
    and the exit goes through — the hold is re-checked, just not every 60 s."""
    c = world
    ref = _open(c)
    served = iter([DEAD, OK])
    calls = []
    fn = lambda t, x: calls.append(x) or next(served)
    statuses = [_tick_at(c, datetime(2026, 10, 27, 11, m), fn, 5000.0 + 60 * m)["exits"][0]["status"]
                for m in range(6)]
    assert statuses == ["held_loss_beyond_max"] * 5 + ["settled"] and len(calls) == 2
    assert lp.has_open_position(c, LIVE, ref) is False and not _lock_active(c, ref)


def test_a_different_predicate_on_the_cached_snapshot_is_still_re_verified(world, monkeypatch):
    c = world
    _open(c)
    calls = []
    fn = lambda t, x: calls.append(x) or DEAD
    _tick_at(c, datetime(2026, 10, 27, 11, 0), fn, 5000.0)
    t = _tick_at(c, datetime(2026, 10, 27, 11, 1), fn, 5060.0)
    assert len(calls) == 1 and t["exits"][0]["held_on_cached_chain"] is True
    real = lp.evaluate
    monkeypatch.setattr(lp, "evaluate", lambda row, chain, today: dict(real(row, chain, today), signal="profit_take"))
    t = _tick_at(c, datetime(2026, 10, 27, 11, 2), fn, 5120.0)
    assert len(calls) == 2                               # new evidence: fetched, as before
    assert t["exits"][0]["signal"] == "profit_take" and "held_on_cached_chain" not in t["exits"][0]


def test_standing_hold_rules():
    row = {"account_id": LIVE, "journal_ref": "sh01", "last_exit_attempt_ts": None}
    src, newer = (1.0, "a", {}), (2.0, "b", {})
    held = {"status": "held_loss_beyond_max", "journal_ref": "sh01", "would_loss_ps": 75.0, "max_loss_ps": 70.0}
    lp.reset_cache()
    now = datetime(2026, 10, 27, 11, 1)
    try:
        assert lp._standing_hold(row, {"signal": "pre_expiry_exit"}, src, now, 300) is None
        lp._HELD_EXIT[(LIVE, "sh01")] = {"src": src, "signal": "pre_expiry_exit", "res": held}
        assert lp._standing_hold(row, {"signal": "pre_expiry_exit"}, src, now, 300) == \
            dict(held, held_on_cached_chain=True)
        assert lp._standing_hold(row, {"signal": "pre_expiry_exit"}, newer, now, 300) is None   # a newer snapshot
        assert lp._standing_hold(row, {"signal": "ratchet_hit"}, src, now, 300) is None         # another predicate
        recent = dict(row, last_exit_attempt_ts="2026-10-27T10:56:01")
        assert lp._standing_hold(recent, {"signal": "ratchet_hit"}, newer, now, 300)["status"] == "held_recent_attempt"
        due = dict(row, last_exit_attempt_ts="2026-10-27T10:56:00")                          # exactly 300 s
        assert lp._standing_hold(due, {"signal": "ratchet_hit"}, newer, now, 300) is None
    finally:
        lp.reset_cache()
    assert lp._HELD_EXIT == {}


# =================================================================== F07 — the audit event commits with the money

def _fail_live_exit_inserts(c):
    c.execute("CREATE TEMP TRIGGER l2_f07 BEFORE INSERT ON paper_account_events "
              "WHEN NEW.event_type = 'live_exit' BEGIN SELECT RAISE(ABORT, 'database is locked'); END")
    c.commit()


def _heal(c):
    c.execute("DROP TRIGGER l2_f07")
    c.commit()


def test_a_failed_live_exit_event_books_nothing_and_the_next_tick_books_both(world, capsys):
    c = world
    ref = _open(c)
    before = _realized(c)
    _fail_live_exit_inserts(c)
    t = _tick_at(c, datetime(2026, 10, 27, 11, 0), OK, 5000.0)
    (x,) = t["exits"]
    assert x["status"] == "exit_error" and "rolled back" in x["reason"] and "nothing booked" in x["reason"]
    assert "tick failed" not in capsys.readouterr().out
    (tid, st), = _exit_tickets(c)
    assert st == oms.FILLED and x["ticket_id"] == tid
    assert _row(c)["state"] == "exiting" and _lock_active(c, ref) and _realized(c) == before
    assert _events(c, lp.EVENT_EXIT) == 0
    _heal(c)
    t = _tick_at(c, datetime(2026, 10, 27, 11, 1), OK, 5060.0)
    (r,) = t["resumes"]
    assert r["status"] == "settled" and r["ticket_id"] == tid and r["exit_mark_ps"] == 64.0
    (ev,) = [json.loads(d) for d in _event_details(c, lp.EVENT_EXIT)]
    assert ev["pnl_net"] == r["pnl_net"] and ev["lock_released"] is True and ev["resolution"] == "pre_expiry_exit"
    assert not _lock_active(c, ref) and _realized(c) == pytest.approx(before + r["pnl_net"])
    assert lp.positions(c)[0]["pnl_net"] == r["pnl_net"]


def test_a_backstop_settlement_whose_event_fails_books_nothing_and_the_next_run_books_both(world):
    c = world
    ref = _open(c)
    _fail_live_exit_inserts(c)
    out = lp.eod_sweep(c, today=date(2026, 10, 29), bars_fn=lambda t, s: _bars(24150.0), now=EOD)
    assert out["settled"] == [] and out["errors"] == [f"{ref}: database is locked"]
    assert _row(c)["state"] == "open" and _lock_active(c, ref) and _events(c, lp.EVENT_EXIT) == 0
    _heal(c)
    out = lp.eod_sweep(c, today=date(2026, 10, 29), bars_fn=lambda t, s: _bars(24150.0), now=EOD)
    (s,) = out["settled"]
    assert json.loads(_event_details(c, lp.EVENT_EXIT)[0])["pnl_net"] == s["pnl_net"]
    assert not _lock_active(c, ref)


def test_a_failed_journal_stamp_is_reported_settled_and_re_stamped_by_the_backstop(world, monkeypatch, capsys):
    c = world
    ref = _open(c)
    monkeypatch.setattr(lp, "_is_test_env", lambda: False)     # the real stamp, on the tmp journal (conftest)
    monkeypatch.setattr(lp, "_stamp_journal", REAL_STAMP)
    journal.log({"short_id": ref, "ticker": "NIFTY 50", "date": "2026-09-29", "decision": "approved",
                 "outcome": None, "accounts": {LIVE: {"status": "approved", "lots": 1}}})
    real_update = journal.update_entry
    monkeypatch.setattr(journal, "update_entry",
                        lambda *a, **k: (_ for _ in ()).throw(journal.JournalLockTimeout("journal lock held")))
    t = _tick_at(c, datetime(2026, 10, 27, 11, 0), OK, 5000.0)
    (x,) = t["exits"]
    assert x["status"] == "settled"                           # the money settled: said so
    out = capsys.readouterr().out
    assert f"journal stamp skipped for {ref}" in out and "tick failed" not in out
    assert journal.get_entry(ref)["accounts"][LIVE]["status"] == "approved"
    monkeypatch.setattr(journal, "update_entry", real_update)
    swept = lp.eod_sweep(c, today=date(2026, 10, 27), bars_fn=lambda t, s: [], now=datetime(2026, 10, 27, 12, 0))
    assert swept["restamped"] == [ref]
    block = journal.get_entry(ref)["accounts"][LIVE]
    assert block["status"] == "closed" and block["pnl_rs"] == x["pnl_net"]
    assert block["resolution"] == "pre_expiry_exit" and block["closed_at"] == "2026-10-27T11:00:00"
    again = lp.eod_sweep(c, today=date(2026, 10, 27), bars_fn=lambda t, s: [], now=datetime(2026, 10, 27, 13, 0))
    assert "restamped" not in again                           # read against the journal: never twice


def test_the_stamp_repair_leaves_rows_the_journal_has_no_block_for(world, monkeypatch):
    c = world
    ref = _open(c)
    monkeypatch.setattr(lp, "_is_test_env", lambda: False)
    stamped = []
    monkeypatch.setattr(lp, "_stamp_journal", lambda row, payload: stamped.append(row["journal_ref"]) or True)
    assert _tick_at(c, datetime(2026, 10, 27, 11, 0), OK, 5000.0)["exits"][0]["status"] == "settled"
    journal.log({"short_id": ref, "ticker": "NIFTY 50", "date": "2026-09-29", "decision": "approved",
                 "outcome": None, "accounts": {TWO_L: {"status": "approved"}}})
    assert lp._repair_journal_stamps(c) == [] and stamped == [ref]          # only the settle's own stamp


# =================================================================== F08 — the backstop resolves 'exiting' rows

class _FillThenRaise:
    """The venue fills the exit ticket, then the door dies before reporting it."""
    @staticmethod
    def sweep(conn, **kw):
        pv.sweep(conn, **kw)
        raise sqlite3.OperationalError("database is locked")


@pytest.mark.parametrize("close, long_px, beyond", [(24100.0, 100.0, False), (24350.0, 350.0, True)])
def test_a_basket_cut_on_expiry_day_after_the_cutoff_settles_on_the_backstop(world, monkeypatch, close, long_px,
                                                                              beyond):
    """The L1 deploy gate: a pre-expiry basket cut between its legs at 15:26
    on expiry day (the short bought back @60, the long still open); past
    the 15:27 cutoff no chain is fetched, and after expiry there is none.
    The backstop waits for the expiry session's close, then books the short
    at its fill and the long at intrinsic — unclamped, named when beyond."""
    c = world
    ref = _open(c)
    real_fill = _fail_second_leg_fill(monkeypatch)
    assert lp._exit(c, _row(c), PX, "pre_expiry_exit", datetime(2026, 10, 28, 15, 26))["status"] == "exit_error"
    monkeypatch.setattr(oms, "apply_fill", real_fill)
    t = _tick_at(c, datetime(2026, 10, 28, 15, 28), LATER, 9000.0)
    assert t["resumes"][0]["status"] == "partial" and t["exits"] == []
    assert "past the fetch cutoff" in t["row_notes"][0]["reason"]
    r = _row(c)
    assert r["state"] == "exiting" and _lock_active(c, ref)
    # 00:35: no expiry-session bar yet — the row waits, named, lock held
    out = lp.eod_sweep(c, today=date(2026, 10, 29), bars_fn=lambda t, s: UPTO_27, now=datetime(2026, 10, 29, 0, 35))
    assert out["waiting"] == [ref] and out["settled"] == []
    assert out["reasons"][ref].startswith("NIFTY 50 expired 2026-10-28 and the expiry session's own close "
                                          "(2026-10-28) has not arrived yet — the newest close is 2026-10-27")
    assert "left 'exiting' by an unfinished exit" in out["reasons"][ref]
    assert [(x["journal_ref"], x["status"]) for x in out["exiting"]] == [(ref, "waiting")]
    assert _row(c)["state"] == "exiting" and _lock_active(c, ref)
    # 05:35: the expiry bar is in
    out = lp.eod_sweep(c, today=date(2026, 10, 29), bars_fn=lambda t, s: _bars(close), now=EOD)
    (s,) = out["settled"]
    profit = long_px - 60.0 - 70.0
    prices = {(24000.0, "CE"): long_px, (24200.0, "CE"): 60.0}
    assert s["resolution"] == "expiry_backstop" and s["basis"] == "last_close_on_or_before_expiry"
    assert s["exit_resolution"] == "pre_expiry_exit" and s["settlement_close_date"] == "2026-10-28"
    assert s["exit_mark_ps"] == long_px - 60.0 and s["profit_ps"] == profit
    assert s["pnl_net"] == round(profit * 65 - lp._frictions(r, prices), 2)
    assert s["partial"] == {"closed_at_fills": {"SELL 24200CE": 60.0}, "open_at_expiry": {"BUY 24000CE": long_px}}
    assert ("legged_beyond_bounds" in s) is beyond
    assert out["exiting"] == [{"journal_ref": ref, "status": "settled", "reason": lp._exiting_how(s)}]
    assert lp.positions(c)[0]["state"] == "closed" and lp.positions(c)[0]["pnl_net"] == s["pnl_net"]
    assert not _lock_active(c, ref) and _realized(c) == s["pnl_net"]
    assert _events(c, lp.EVENT_EXIT) == 1 and len(_exit_tickets(c)) == 1      # nothing was sold at expiry


def test_an_exiting_row_with_nothing_filled_settles_like_an_open_row(world):
    c = world
    ref = _open(c)
    assert lp._exit(c, _row(c), PX, "pre_expiry_exit", datetime(2026, 10, 28, 15, 20),
                    venue_mod=_Raise)["status"] == "exit_error"
    (tid, st), = _exit_tickets(c)
    assert st == oms.PENDING
    r = _row(c)
    out = lp.eod_sweep(c, today=date(2026, 10, 29), bars_fn=lambda t, s: _bars(24350.0), now=EOD)
    (s,) = out["settled"]
    assert s["cancelled"] == {tid: oms.CANCELLED} and oms.ticket_status(c, tid) == oms.CANCELLED
    assert s["exit_mark_ps"] == 200.0 and s["profit_ps"] == 130.0                  # clamped, like an open row
    assert s["pnl_net"] == round(130.0 * 65 - lp._frictions(r, {(24000.0, "CE"): 350.0, (24200.0, "CE"): 150.0}), 2)
    assert "partial" not in s and "legged_beyond_bounds" not in s
    assert out["exiting"][0]["reason"].startswith("nothing had filled: settled last_close_on_or_before_expiry")
    assert not _lock_active(c, ref)


def test_an_exiting_row_whose_exit_filled_settles_from_its_fills_without_bars(world):
    c = world
    ref = _open(c)
    assert lp._exit(c, _row(c), PX, "pre_expiry_exit", datetime(2026, 10, 28, 15, 20),
                    venue_mod=_FillThenRaise)["status"] == "exit_error"
    assert [s for _, s in _exit_tickets(c)] == [oms.FILLED] and _row(c)["state"] == "exiting"
    out = lp.eod_sweep(c, today=date(2026, 10, 29), bars_fn=lambda t, s: [], now=EOD)   # no bar needed
    (s,) = out["settled"]
    assert s["resolution"] == "pre_expiry_exit" and s["basis"] == "live_bid_ask" and s["exit_mark_ps"] == 90.0
    assert s["pnl_net"] == round(20.0 * 65 - lp._frictions(_row_closed(c), PX), 2)
    assert not _lock_active(c, ref) and out["waiting"] == []


def _row_closed(c, ref="lx0001"):
    return [r for r in lp.positions(c) if r["journal_ref"] == ref][0]


def test_with_no_price_data_after_grace_the_open_legs_take_their_conservative_bound(world, monkeypatch):
    """No close at all, 3 days past expiry: the open long is worth 0 (it can
    never be worth less) — booked from the short's real fill, unclamped."""
    c = world
    ref = _open(c)
    _cut_basket(c, monkeypatch)
    r = _row(c)
    out = lp.eod_sweep(c, today=date(2026, 10, 30), bars_fn=lambda t, s: [], now=datetime(2026, 10, 30, 5, 35))
    assert out["waiting"] == [ref]                                               # inside the grace window
    out = lp.eod_sweep(c, today=date(2026, 10, 31), bars_fn=lambda t, s: [], now=datetime(2026, 10, 31, 5, 35))
    (s,) = out["settled"]
    assert s["basis"] == "no_price_data_max_loss" and s["partial"]["open_at_expiry"] == {"BUY 24000CE": 0.0}
    assert s["exit_mark_ps"] == -60.0 and s["profit_ps"] == -130.0 and "legged_beyond_bounds" in s
    assert s["pnl_net"] == round(-130.0 * 65 - lp._frictions(r, {(24000.0, "CE"): 0.0, (24200.0, "CE"): 60.0}), 2)
    assert not _lock_active(c, ref)


def test_with_no_price_data_an_open_short_is_valued_at_the_structure_width(world, monkeypatch):
    c = world
    ref = _open(c, "lx0003", _condor(), CONDOR_ENTRY)
    real = _fail_second_leg_fill(monkeypatch)           # the condor basket is cut after its FIRST leg
    assert lp._exit(c, _row(c, ref), CONDOR_PX, "profit_take", OPEN)["status"] == "exit_error"
    monkeypatch.setattr(oms, "apply_fill", real)
    out = lp.eod_sweep(c, today=date(2026, 10, 31), bars_fn=lambda t, s: [], now=datetime(2026, 10, 31, 5, 35))
    (s,) = out["settled"]
    opened = s["partial"]["open_at_expiry"]
    assert len(opened) == 3 and len(s["partial"]["closed_at_fills"]) == 1
    assert {k: v for k, v in opened.items() if k.startswith("SELL")} == {"SELL 24500CE": 200.0} or \
        {k: v for k, v in opened.items() if k.startswith("SELL")} == {"SELL 23500PE": 200.0}
    assert all(v == 0.0 for k, v in opened.items() if k.startswith("BUY"))
    assert s["profit_ps"] < -float(_row_closed(c, ref)["max_loss_ps"]) and "legged_beyond_bounds" in s
    assert not _lock_active(c, ref)


def test_the_backstop_settles_an_open_row_only_in_the_state_it_read(world, monkeypatch):
    c = world
    ref = _open(c)
    real = pt._expiry_backstop

    def stamped_meanwhile(entry, bars, today):            # a tick stamps it 'exiting' under the sweep
        c.execute("UPDATE paper_live_positions SET state = 'exiting', exit_started_at = '2026-10-28T15:20:00', "
                  "last_exit_attempt_ts = '2026-10-28T15:20:00' WHERE journal_ref = ?", (ref,))
        c.commit()
        return real(entry, bars, today)
    monkeypatch.setattr(pt, "_expiry_backstop", stamped_meanwhile)
    out = lp.eod_sweep(c, today=date(2026, 10, 29), bars_fn=lambda t, s: _bars(24350.0), now=EOD)
    assert out["settled"] == [] and out["errors"] == []
    assert out["moved"] == [f"{ref}: row is 'exiting' now, not 'open' as read — another actor moved it; "
                            "nothing settled"]
    assert _row(c)["state"] == "exiting" and _lock_active(c, ref) and _events(c, lp.EVENT_EXIT) == 0


def test_the_backstop_settles_an_exiting_row_only_in_the_state_it_read(world, monkeypatch):
    c = world
    ref = _open(c)
    assert lp._exit(c, _row(c), PX, "pre_expiry_exit", datetime(2026, 10, 28, 15, 20),
                    venue_mod=_Raise)["status"] == "exit_error"
    snapshot = _row(c)
    real_cancel = oms.cancel_ticket

    def cancel_then_reopened(conn, ticket_id, reason="cancelled"):   # a resume reopens it under the sweep
        res = real_cancel(conn, ticket_id, reason)
        assert lp._reopen(c, snapshot, snapshot["exit_ticket_id"], snapshot["exit_started_at"])
        return res
    monkeypatch.setattr(oms, "cancel_ticket", cancel_then_reopened)
    out = lp.eod_sweep(c, today=date(2026, 10, 29), bars_fn=lambda t, s: _bars(24350.0), now=EOD)
    assert out["settled"] == [] and out["exiting"][0]["status"] == "state_moved"
    assert "row is 'open' now, not 'exiting' as read" in out["exiting"][0]["reason"]
    assert _row(c)["state"] == "open" and _lock_active(c, ref) and _events(c, lp.EVENT_EXIT) == 0


def test_the_backstop_takes_the_completions_claim_so_only_one_acts(two, monkeypatch):
    """eod_sweep (the API process) has no tick lock: while a completion is
    inside the exit door, the sweep's claim on the same row is refused —
    and while the sweep holds it, a completion's is (L1 residual (a))."""
    a, b = two
    ref = _open(a)
    _cut_basket(a, monkeypatch)
    real, calls = pt._execute_paper_exit, {"n": 0}

    def completion_inside_the_door(*args, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            calls["sweep"] = lp.eod_sweep(b, today=date(2026, 10, 29), bars_fn=lambda t, s: _bars(24350.0),
                                          now=datetime(2026, 10, 29, 9, 31))
        return real(*args, **kw)
    monkeypatch.setattr(pt, "_execute_paper_exit", completion_inside_the_door)
    res = lp._complete_exit(a, _row(a), LATER, "q", datetime(2026, 10, 29, 9, 30))
    sw = calls["sweep"]
    assert sw["settled"] == [] and sw["exiting"][0]["status"] == "partial_held"
    assert "already claimed at 2026-10-29T09:30:00" in sw["exiting"][0]["reason"]
    assert res["status"] == "settled" and calls["n"] == 1
    assert _exit_qty_by_leg(a) == {("BUY", 24200.0, "CE"): 65, ("SELL", 24000.0, "CE"): 65}
    assert _events(a, lp.EVENT_EXIT, ref) == 1


def test_a_completion_is_refused_while_the_backstop_holds_the_claim(world, monkeypatch):
    c = world
    ref = _open(c)
    _cut_basket(c, monkeypatch)
    real, seen = lp._booked_fills, {}

    def completion_meanwhile(ex, attempt=None):           # after the sweep's claim, before its settle
        if "res" not in seen:
            seen["res"] = lp._complete_exit(c, _row(c), LATER, "q", datetime(2026, 10, 29, 5, 36))
        return real(ex, attempt)
    monkeypatch.setattr(lp, "_booked_fills", completion_meanwhile)
    out = lp.eod_sweep(c, today=date(2026, 10, 29), bars_fn=lambda t, s: _bars(24350.0), now=EOD)
    assert seen["res"]["status"] == "partial_held" and "already claimed at 2026-10-29T05:35:00" in seen["res"]["reason"]
    assert len(out["settled"]) == 1 and len(_exit_tickets(c)) == 1
    assert not _lock_active(c, ref)


def test_run_tracker_names_what_the_backstop_did_with_an_exiting_row(world, monkeypatch, capsys):
    c = world
    _open(c)
    assert lp._exit(c, _row(c), PX, "pre_expiry_exit", datetime(2026, 10, 28, 15, 20),
                    venue_mod=_Raise)["status"] == "exit_error"
    monkeypatch.setattr(lp, "eod_sweep_standalone",
                        lambda today=None: lp.eod_sweep(c, today=date(2026, 10, 29),
                                                        bars_fn=lambda t, s: _bars(24350.0), now=EOD))
    monkeypatch.setattr(pt.journal, "read_all", lambda: [])
    assert pt.run_tracker(email=False) == 0
    out = capsys.readouterr().out
    assert ("Plan tracker: live account lx0001 was left 'exiting' past expiry — settled: nothing had filled: "
            "settled last_close_on_or_before_expiry like an open row") in out


# =================================================================== F15 — a FILLED entry is never 'never opened'

def _filled_live_entry_without_a_row(c, ref, fill=True):
    """The approval's state after open_position failed on a FILLED ticket:
    lock active, entry ticket FILLED, no position row."""
    e = {"short_id": ref, "date": "2026-09-29", "ticker": "NIFTY 50", "spread": _bull_call(), "signal": "t"}
    pm.request_entry(c, ref, 17550.0)
    pm.paper_request_entry(c, LIVE, ref, 17550.0, lots=1, primary_lots=1)
    rq = lp.requote_entry(e, now=OPEN, chain_fn=lambda t, x: ENTRY)
    prop = {"ticker": e["ticker"], "short_id": ref, "signal": "t", "spread": dict(e["spread"], legs=rq["legs"])}
    issued = sr.issue(c, prop, journal_ref=ref, source="t", account_id=LIVE, lots=1)
    if fill:
        pv.sweep(c, stamp=False)
    else:
        oms.cancel_ticket(c, issued["ticket_id"], "unfilled")
    return issued["ticket_id"]


def test_a_filled_live_entry_with_no_row_keeps_its_lock_when_the_primary_settles_first(world):
    c = world
    ref = "lx0015"
    tid = _filled_live_entry_without_a_row(c, ref)
    assert oms.ticket_status(c, tid) == oms.FILLED and not lp.has_open_position(c, LIVE, ref)
    res = pm.release_entry(ref, 5000.0, conn=c, wealth_sweep=False)
    v = res["shadow_accounts"][LIVE]
    assert v["released"] is False and f"entry ticket {tid} FILLED" in v["reason"] and "lock kept" in v["reason"]
    assert _lock_active(c, ref) and _events(c, lp.EVENT_LOCK_KEPT_FILLED, ref) == 1
    assert _events(c, lp.EVENT_LOCK_NO_POSITION, ref) == 0
    pm.release_entry(ref, 5000.0, conn=c, wealth_sweep=False)          # the hourly reconcile retries
    assert _lock_active(c, ref) and _events(c, lp.EVENT_LOCK_KEPT_FILLED, ref) == 1      # one row a day
    t = _tick_at(c, OPEN.replace(minute=5), ENTRY, 1000.0)            # the one repair door opens it
    assert t["repaired"] == [ref] and lp.has_open_position(c, LIVE, ref)
    out = lp.eod_sweep(c, today=date(2026, 10, 29), bars_fn=lambda t, s: _bars(24350.0), now=EOD)
    (s,) = out["settled"]                                              # ... and it settles on its own
    assert s["profit_ps"] == 130.0 and s["pnl_net"] > 0 and _row_closed(c, ref)["pnl_net"] == s["pnl_net"]
    assert not _lock_active(c, ref) and _realized(c) == s["pnl_net"]


def test_a_lock_whose_entry_never_filled_is_still_released_at_zero(world):
    c = world
    ref = "lx0016"
    tid = _filled_live_entry_without_a_row(c, ref, fill=False)
    assert oms.ticket_status(c, tid) == oms.CANCELLED
    v = pm.release_entry(ref, 5000.0, conn=c, wealth_sweep=False)["shadow_accounts"][LIVE]
    assert v["released"] is True and v["pnl_net"] == 0.0
    assert _events(c, lp.EVENT_LOCK_NO_POSITION, ref) == 1 and _events(c, lp.EVENT_LOCK_KEPT_FILLED, ref) == 0
    assert lp.filled_entry_ticket(c, LIVE, ref) is None


def test_no_oms_tables_means_no_filled_entry():
    c = brain_map.connect(":memory:")
    try:
        assert lp.filled_entry_ticket(c, LIVE, "x") is None
        assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'trade_tickets'").fetchone()[0] == 0
    finally:
        c.close()


# =================================================================== F16 — expire_pending_lock is one commit

@pytest.fixture
def pending_db(monkeypatch, tmp_path):
    db = tmp_path / "f16.db"
    c = brain_map.connect(str(db))
    pm.ensure_accounts_schema(c)
    oms.ensure_schema(c)
    lp.ensure_schema(c)
    pm.get_account(c)
    monkeypatch.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", False)
    ref = "f16pend"
    assert pm.request_entry(c, ref, 17550.0)["approved"]
    for acct in (TWO_L, LIVE):
        assert pm.paper_request_entry(c, acct, ref, 17550.0, lots=1, primary_lots=1)["approved"]
    # a persistent trigger, visible to the connection the production door opens
    c.execute("CREATE TRIGGER l2_f16 BEFORE INSERT ON paper_account_events WHEN NEW.account_id = 'PAPER_2L_LIVE' "
              "AND NEW.event_type = 'pending_lock_expired' BEGIN SELECT RAISE(ABORT, 'simulated busy/disk'); END")
    c.commit()
    c.close()
    monkeypatch.setattr(pt, "_brain_connect", lambda: brain_map.connect(str(db)))
    journal.log({"short_id": ref, "ticker": "NIFTY", "decision": "pending_approval", "outcome": None,
                 "date": "2026-10-05", "created_at": "2026-10-05T10:00:00+05:30"})
    return db, ref


def _expiry_state(db, ref):
    c = brain_map.connect(str(db))
    try:
        prim = c.execute("SELECT released_at FROM margin_locks WHERE journal_ref = ?", (ref,)).fetchone()[0]
        shadows = dict(tuple(r) for r in c.execute("SELECT account_id, released_at FROM paper_margin_locks "
                                                   "WHERE journal_ref = ?", (ref,)).fetchall())
        events = c.execute("SELECT COUNT(*) FROM paper_account_events WHERE event_type = ?",
                           (pm.PENDING_LOCK_EXPIRED_EVENT,)).fetchone()[0] + \
            c.execute("SELECT COUNT(*) FROM account_events WHERE event_type = ?",
                      (pm.PENDING_LOCK_EXPIRED_EVENT,)).fetchone()[0]
        return prim, shadows, events
    finally:
        c.close()


def test_a_failed_live_expiry_expires_nothing_and_the_next_run_expires_all(pending_db, monkeypatch):
    db, ref = pending_db
    seen, real = [], lp.has_open_position

    def spy(conn, account, journal_ref):
        before = conn.in_transaction
        held = real(conn, account, journal_ref)
        seen.append((account, before, conn.in_transaction))
        return held
    monkeypatch.setattr(lp, "has_open_position", spy)
    now = datetime(2026, 10, 5, 15, 31, tzinfo=pm.IST)
    with pytest.raises(Exception, match="simulated busy/disk"):
        pt.expire_pending_margin(now=now)
    assert seen == [(LIVE, True, True)]              # the guard read INSIDE the transaction and committed nothing
    assert _expiry_state(db, ref) == (None, {TWO_L: None, LIVE: None}, 0)           # NOTHING expired
    c = brain_map.connect(str(db))
    c.execute("DROP TRIGGER l2_f16")
    c.commit()
    c.close()
    out = pt.expire_pending_margin(now=now)
    assert out == {ref: {pm.ACCOUNT_PAPER_10L: 17550.0, TWO_L: 17550.0, LIVE: 17550.0}}
    prim, shadows, events = _expiry_state(db, ref)
    assert prim is not None and all(v is not None for v in shadows.values()) and events == 3


def test_a_failed_expiry_rolls_back_on_the_callers_own_connection(pending_db):
    """The door rolls its own transaction back: a caller that keeps using
    the connection (no close in between) sees nothing expired either."""
    db, ref = pending_db
    c = brain_map.connect(str(db))
    try:
        with pytest.raises(Exception, match="simulated busy/disk"):
            pm.expire_pending_lock(c, ref, why="test")
        assert c.in_transaction is False
        assert c.execute("SELECT released_at FROM margin_locks WHERE journal_ref = ?", (ref,)).fetchone()[0] is None
        assert c.execute("SELECT COUNT(*) FROM paper_margin_locks WHERE journal_ref = ? AND released_at IS NOT NULL",
                         (ref,)).fetchone()[0] == 0
    finally:
        c.close()


def test_the_live_schema_check_never_commits_a_callers_transaction(world):
    c = world
    c.execute("INSERT INTO paper_account_events (account_id, ts, event_type) VALUES ('X', 't', 'l2_probe')")
    assert c.in_transaction
    lp.ensure_schema(c)
    assert lp.has_open_position(c, LIVE, "nope") is False and lp.closed_pnl(c, LIVE, "nope") is None
    assert c.in_transaction
    c.rollback()
    assert c.execute("SELECT COUNT(*) FROM paper_account_events WHERE event_type = 'l2_probe'").fetchone()[0] == 0


def test_the_live_schema_is_still_created_on_a_fresh_database():
    c = brain_map.connect(":memory:")
    try:
        assert lp.open_rows(c) == [] and lp.has_open_position(c, LIVE, "x") is False
        assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'paper_live_positions'").fetchone()[0] == 1
    finally:
        c.close()


# =================================================================== L1 residual (a) — the claim is exclusive

def test_a_completer_that_read_the_row_after_anothers_claim_is_refused(two, monkeypatch):
    """B's tick reads the row AFTER A's claim committed (so B's snapshot is
    current) but BEFORE A's ticket exists. Matching the read alone let B
    claim and sell the open long a second time; the in-flight test inside
    the compare-and-set refuses it."""
    a, b = two
    ref = _open(a)
    t1 = _cut_basket(a, monkeypatch)                  # last attempt 11:01
    real, calls = pt._execute_paper_exit, {"n": 0}

    def a_inside_the_door(*args, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            fresh_b = _row(b)
            calls["read"] = fresh_b["last_exit_attempt_ts"]
            calls["b"] = lp._complete_exit(b, fresh_b, LATER, "2026-09-29T11:07:00", OPEN.replace(minute=7))
        return real(*args, **kw)
    monkeypatch.setattr(pt, "_execute_paper_exit", a_inside_the_door)
    res_a = lp._complete_exit(a, _row(a), LATER, "2026-09-29T11:06:00", OPEN.replace(minute=6))
    assert calls["read"] == "2026-09-29T11:06:00"                               # B saw A's claim
    assert calls["n"] == 1 and calls["b"]["status"] == "partial_held"            # B never reached the door
    assert "already claimed at 2026-09-29T11:06:00" in calls["b"]["reason"]
    assert res_a["status"] == "settled" and res_a["exit_tickets"] == [t1, _exit_tickets(a)[1][0]]
    assert _exit_qty_by_leg(a) == {("BUY", 24200.0, "CE"): 65, ("SELL", 24000.0, "CE"): 65}   # sold ONCE
    assert _events(a, lp.EVENT_EXIT, ref) == 1


def test_a_claimant_whose_read_of_the_last_claim_is_stale_is_refused_even_after_the_interval(world, monkeypatch):
    """The claim is a compare-and-set on the row AS READ: a claim that went
    nowhere (a door error, no ticket) still moved last_exit_attempt_ts, so a
    snapshot read before it is stale — refused, re-read on the next tick."""
    c = world
    _open(c)
    _cut_basket(c, monkeypatch)                       # last attempt 11:01
    stale = _row(c)
    assert lp._claim(c, _row(c), datetime(2026, 9, 29, 11, 6, 0), 300)["owned"] is True   # a claim that issued nothing
    got = lp._claim(c, stale, datetime(2026, 9, 29, 11, 12, 0), 300)
    assert got["owned"] is False and got["status"] == "exit_not_owned"
    assert lp._claim(c, _row(c), datetime(2026, 9, 29, 11, 12, 0), 300)["owned"] is True  # a fresh read claims


@pytest.mark.parametrize("when, owned", [(datetime(2026, 9, 29, 11, 5, 59), False),
                                         (datetime(2026, 9, 29, 11, 6, 0), True)])
def test_a_claim_needs_one_full_quote_interval_since_the_last_attempt(world, monkeypatch, when, owned):
    c = world
    _open(c)
    _cut_basket(c, monkeypatch)                       # last attempt 11:01:00
    got = lp._claim(c, _row(c), when, 300)
    assert got["owned"] is owned
    if owned:
        assert _row(c)["last_exit_attempt_ts"] == lp._iso(when)
    else:
        assert got["status"] == "partial_held" and _row(c)["last_exit_attempt_ts"] == "2026-09-29T11:01:00"


# =================================================================== L1 residual (b) — floored legs on every ticket

def _cut_condor_after_a_wing(c, monkeypatch, ref):
    """A condor basket cut after both shorts and ONE worthless wing (floored
    to 0.05) filled; the resume leaves it a standing partial."""
    real, calls = oms.apply_fill, {"n": 0}

    def flaky(conn, leg_id, *a, **k):
        calls["n"] += 1
        if calls["n"] == 4:
            raise sqlite3.OperationalError("database is locked")
        return real(conn, leg_id, *a, **k)
    monkeypatch.setattr(oms, "apply_fill", flaky)
    assert lp._exit(c, _row(c, ref), CONDOR_PX, "profit_take", OPEN)["status"] == "exit_error"
    monkeypatch.setattr(oms, "apply_fill", real)
    assert lp._resume_exiting(c, _row(c, ref), OPEN.replace(minute=1))["status"] == "partial"


def test_a_wing_floored_on_the_first_ticket_is_booked_at_its_quote(world, monkeypatch):
    c = world
    ref = _open(c, "lx0003", _condor(), CONDOR_ENTRY)
    _cut_condor_after_a_wing(c, monkeypatch, ref)
    zero = _chain({(23500, "PE"): (4.0, 5.0, 4.5), (23300, "PE"): (None, 1.0, None),
                   (24500, "CE"): (4.0, 5.0, 4.5), (24700, "CE"): (None, 1.0, None)})
    res = lp._complete_exit(c, _row(c, ref), zero, "q", OPEN.replace(minute=9))
    assert res["status"] == "settled" and len(res["exit_tickets"]) == 2
    assert res["exit_mark_ps"] == -10.0                    # both wings at 0, the shorts at 5 — not -9.95
    assert res["fills"]["23300PE"] == 0.05 and res["fills"]["24700CE"] == 0.05
    assert res["limit_floored"] == {"23300PE": 0.05, "24700CE": 0.05}


def test_a_crash_resumed_floored_exit_is_booked_at_the_quote(world):
    """No attempt in memory knows which legs were floored: the ticket's own
    limit says so (L1's accepted residual, now closed)."""
    c = world
    ref = _open(c, "lx0003", _condor(), CONDOR_ENTRY)
    assert lp._exit(c, _row(c, ref), CONDOR_PX, "profit_take", OPEN, venue_mod=_Raise)["status"] == "exit_error"
    (tid, _), = _exit_tickets(c, ref)
    for l in oms.ticket_view(c, tid)["legs"]:
        assert oms.apply_fill(c, l["leg_id"], l["qty_target"], l["limit_price"])["ok"]
    res = lp._resume_exiting(c, _row(c, ref), OPEN.replace(minute=1))
    assert res["status"] == "settled" and res["exit_mark_ps"] == -10.0          # not -9.9
    assert res["limit_floored"] == {"23300PE": 0.05, "24700CE": 0.05}


def test_a_long_filled_at_005_below_a_higher_limit_is_booked_at_its_fill(world):
    """Only a fill at a limit that was itself floored is re-priced: wings
    quoted at 0.10 (limit 0.10) that filled at 0.05 really sold at 0.05."""
    c = world
    ref = _open(c, "lx0003", _condor(), CONDOR_ENTRY)
    px = {**CONDOR_PX, (23300.0, "PE"): 0.10, (24700.0, "CE"): 0.10}
    assert lp._exit(c, _row(c, ref), px, "profit_take", OPEN, venue_mod=_Raise)["status"] == "exit_error"
    (tid, _), = _exit_tickets(c, ref)
    for l in oms.ticket_view(c, tid)["legs"]:
        assert oms.apply_fill(c, l["leg_id"], l["qty_target"], 0.05 if l["limit_price"] == 0.10 else 5.0)["ok"]
    res = lp._resume_exiting(c, _row(c, ref), OPEN.replace(minute=1))
    assert res["status"] == "settled" and res["exit_mark_ps"] == -9.9 and "limit_floored" not in res


def test_a_real_005_bid_on_this_attempts_own_ticket_is_booked_at_005(world, monkeypatch):
    """The attempt that saw the quotes knows a 0.05 BID from a floored zero."""
    c = world
    _open(c)
    _cut_basket(c, monkeypatch)                            # the short bought back @60
    bid = _chain({(24000, "CE"): (0.05, 0.10, 0.05), (24200, "CE"): (0.5, 0.6, 0.55)})
    res = lp._complete_exit(c, _row(c), bid, "q", OPEN.replace(minute=9))
    assert res["status"] == "settled" and res["exit_mark_ps"] == -59.95 and "limit_floored" not in res


def test_a_short_leg_bought_back_at_005_is_never_re_priced(world):
    c = world
    ref = _open(c)
    cheap = {(24000.0, "CE"): 30.0, (24200.0, "CE"): 0.05}
    assert lp._exit(c, _row(c), cheap, "pre_expiry_exit", OPEN, venue_mod=_Raise)["status"] == "exit_error"
    (tid, _), = _exit_tickets(c, ref)
    for l in oms.ticket_view(c, tid)["legs"]:
        assert oms.apply_fill(c, l["leg_id"], l["qty_target"], l["limit_price"])["ok"]
    res = lp._resume_exiting(c, _row(c), OPEN.replace(minute=1))
    assert res["status"] == "settled" and res["exit_mark_ps"] == 29.95 and "limit_floored" not in res


# =================================================================== L1 residual (c) — surviving mutants pinned

def test_a_cancelled_attempt_with_no_fill_does_not_make_the_next_exit_legged(world):
    """'Legged' counts tickets WITH fills: a withdrawn zero-fill attempt plus
    one filled ticket is a single-ticket exit — clamped, not named."""
    c = world
    _open(c)
    assert lp._exit(c, _row(c), PX, "pre_expiry_exit", OPEN, venue_mod=_NoFill)["status"] == "unfilled"
    row = dict(_row(c), last_exit_attempt_ts=None)
    assert lp._exit(c, row, PX, "pre_expiry_exit", OPEN.replace(minute=10), venue_mod=_Raise)["status"] == "exit_error"
    (_, s1), (t2, _) = _exit_tickets(c)
    assert s1 == oms.CANCELLED
    for l in oms.ticket_view(c, t2)["legs"]:
        assert oms.apply_fill(c, l["leg_id"], l["qty_target"], 5.0 if l["side"] == "SELL" else 60.0)["ok"]
    res = lp._resume_exiting(c, _row(c), OPEN.replace(minute=11))
    assert res["status"] == "settled" and res["exit_mark_ps"] == -55.0
    assert res["profit_ps"] == -70.0 and "exit_tickets" not in res and "legged_beyond_bounds" not in res


def test_a_floored_leg_is_booked_at_its_quote_only_when_it_filled_at_the_floor(world):
    """Crash-resumed condor: one wing filled AT the floor (booked 0), the
    other above it (booked at its fill, 0.10)."""
    c = world
    ref = _open(c, "lx0003", _condor(), CONDOR_ENTRY)
    assert lp._exit(c, _row(c, ref), CONDOR_PX, "profit_take", OPEN, venue_mod=_Raise)["status"] == "exit_error"
    (tid, _), = _exit_tickets(c, ref)
    for l in oms.ticket_view(c, tid)["legs"]:
        px = 0.10 if float(l["strike"]) == 24700.0 else l["limit_price"]
        assert oms.apply_fill(c, l["leg_id"], l["qty_target"], px)["ok"]
    res = lp._resume_exiting(c, _row(c, ref), OPEN.replace(minute=1))
    assert res["exit_mark_ps"] == -9.9 and res["limit_floored"] == {"23300PE": 0.05}


def test_this_attempts_floored_leg_filled_above_the_floor_is_booked_at_its_fill(world, monkeypatch):
    """`_exit`'s own ticket filled under its cancel — the wings above the
    floor (0.10): the attempt's quote (0) applies only to a fill AT it."""
    c = world
    ref = _open(c, "lx0003", _condor(), CONDOR_ENTRY)
    real = oms.cancel_ticket

    def filled_first(conn, ticket_id, reason="cancelled"):
        for l in oms.ticket_view(conn, ticket_id)["legs"]:
            if l["state"] == oms.PENDING:
                oms.apply_fill(conn, l["leg_id"], l["qty_target"],
                               0.10 if l["limit_price"] == lp.TICK_FLOOR else l["limit_price"])
        return real(conn, ticket_id, reason)
    monkeypatch.setattr(oms, "cancel_ticket", filled_first)
    res = lp._exit(c, _row(c, ref), CONDOR_PX, "profit_take", OPEN, venue_mod=_NoFill)
    assert res["status"] == "settled" and res["exit_mark_ps"] == -9.8


@pytest.mark.parametrize("long_bid, profit_ps, named", [(60.0, -70.0, False), (59.95, -70.05, True),
                                                        (260.0, 130.0, False), (260.05, 130.05, True)])
def test_legged_beyond_bounds_is_named_only_strictly_outside_the_bounds(world, monkeypatch, long_bid, profit_ps,
                                                                        named):
    c = world
    _open(c)
    _cut_basket(c, monkeypatch)                            # the short bought back @60; d = 70, bounds [-70, +130]
    moved = _chain({(24000, "CE"): (long_bid, long_bid + 2, long_bid + 1), (24200, "CE"): (1.0, 2.0, 1.5)})
    res = lp._complete_exit(c, _row(c), moved, "q", OPEN.replace(minute=9))
    assert res["status"] == "settled" and res["profit_ps"] == profit_ps
    assert ("legged_beyond_bounds" in res) is named
