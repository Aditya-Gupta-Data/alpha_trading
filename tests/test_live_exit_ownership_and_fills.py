"""
PAPER_2L_LIVE exit serialisation and partial baskets — audit Chunk 2 lows
F05 + F06 (2026-10-06, batch L1).

F06: the live arm's `exiting` stamp is the exit's only serialisation, but
its rowcount was never checked (a stale copy of the row issued and filled a
SECOND exit ticket), and both reopen statements ignored the row's state (a
stale actor reopened a row another one had CLOSED — a ghost whose lock was
already released). Now: the stamp is a checked compare-and-set
(`exit_not_owned`), every reopen is a compare-and-set on the attempt it
undoes (`not_reopened`), and a host-wide flock lets one process tick.

F05: after an exit that did not come back FILLED, the row used to be
reopened WHOLE whatever the OMS said: a basket cut between two per-leg
commits, or a ticket filled under the cancel, was closed a second time
later. Now the decision is made on the OMS's per-leg fills across every
exit ticket of the row — all closed: settle from them; none: reopen; some:
the row stays `exiting` and only the open legs are exited, on a freshly
fetched chain, P&L from both tickets' fills.

Hermetic: sqlite ':memory:' or a tmp_path file (two connections stand in
for two processes), chains injected, the ticket wall clock faked, no
network, nothing written under the real data/ or logs/.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src import brain_map, oms, portfolio_manager as pm
from src import strategy_router as sr
from src.execution import live_pricer as lp, paper_venue as pv
from src.strategy import StrategyConstructor

IST = timezone(timedelta(hours=5, minutes=30))
LIVE = pm.ACCOUNT_PAPER_2L_LIVE
OPEN = datetime(2026, 9, 29, 11, 0)          # a Tuesday, market open
PX = {(24000.0, "CE"): 150.0, (24200.0, "CE"): 60.0}     # sell the long at 150, buy the short back at 60


def _bull_call():
    # BUY 24000 CE @ ask 100 / SELL 24200 CE @ bid 30 -> debit 70, width 200, lot 65
    s = StrategyConstructor(vix=13.0, lot_size=65).construct_bull_call_spread(24000, 24200, 100.0, 30.0)
    for l in s["legs"]:
        l["fill_basis"] = "quoted"
    s.update(lots=1, expiry="2026-10-28", entry_spot=24000.0)
    return s


def _chain(quotes):
    oc = {}
    for (k, t), (bid, ask, ltp) in quotes.items():
        oc.setdefault(f"{float(k):.6f}", {})[t.lower()] = {"top_bid_price": bid, "top_ask_price": ask,
                                                           "last_price": ltp}
    return {"last_price": 24000.0, "oc": oc}


ENTRY = _chain({(24000, "CE"): (98.0, 100.0, 99.0), (24200, "CE"): (30.0, 32.0, 31.0)})
LATER = _chain({(24000, "CE"): (120.0, 122.0, 121.0), (24200, "CE"): (40.0, 42.0, 41.0)})


class _Clock:
    """The exit ticket's wall clock (its id is keyed to the second): one
    second later on every read, as two real attempts always are."""
    t = datetime(2026, 9, 29, 11, 0, tzinfo=IST)


class _FakeDT(datetime):
    @classmethod
    def now(cls, tz=None):
        _Clock.t = _Clock.t + timedelta(seconds=1)
        return _Clock.t if tz is None else _Clock.t.astimezone(tz)


def _setup(c, monkeypatch):
    pm.ensure_accounts_schema(c)
    oms.ensure_schema(c)
    lp.ensure_schema(c)
    pm.get_account(c)
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", False)
    monkeypatch.setattr("src.config.PAPER_VENUE_ENABLED", True)
    monkeypatch.setattr(lp, "_market_open", lambda now: True)
    monkeypatch.setattr(lp, "_now", lambda: OPEN)
    monkeypatch.setattr(lp, "_stamp_journal", lambda row, payload: None)
    monkeypatch.setattr(sr, "datetime", _FakeDT)
    _Clock.t = datetime(2026, 9, 29, 11, 0, tzinfo=IST)
    lp.reset_cache()


@pytest.fixture
def world(monkeypatch):
    c = brain_map.connect(":memory:")
    _setup(c, monkeypatch)
    yield c
    lp.reset_cache()
    c.close()


@pytest.fixture
def two(monkeypatch, tmp_path):
    """Two connections to ONE file: two processes on the VM's brain map."""
    db = tmp_path / "bm.db"
    a = brain_map.connect(db)
    _setup(a, monkeypatch)
    b = brain_map.connect(db)
    yield a, b
    lp.reset_cache()
    b.close()
    a.close()


def _open(c, ref="lx0001"):
    """LIVE's position through the real entry path (requote -> ticket -> venue -> row)."""
    e = {"short_id": ref, "date": "2026-09-29", "ticker": "NIFTY 50", "spread": _bull_call(), "signal": "t"}
    pm.paper_request_entry(c, LIVE, ref, 17550.0, lots=1, primary_lots=1)
    rq = lp.requote_entry(e, now=OPEN, chain_fn=lambda t, x: ENTRY)
    assert rq["ok"], rq
    prop = {"ticker": e["ticker"], "short_id": ref, "signal": "t", "spread": dict(e["spread"], legs=rq["legs"])}
    issued = sr.issue(c, prop, journal_ref=ref, source="t", account_id=LIVE, lots=1)
    pv.sweep(c, stamp=False)
    assert oms.ticket_status(c, issued["ticket_id"]) == oms.FILLED
    lp.open_position(c, LIVE, e, oms.ticket_view(c, issued["ticket_id"]), quote_ts=rq["quote_ts"], now=OPEN)
    return ref


def _row(c, ref="lx0001"):
    return [r for r in lp.open_rows(c, LIVE) if r["journal_ref"] == ref][0]


def _state(c, ref="lx0001"):
    r = c.execute("SELECT state, pnl_net FROM paper_live_positions WHERE account_id = ? AND journal_ref = ?",
                  (LIVE, ref)).fetchone()
    return tuple(r) if r else None


def _exit_tickets(c, ref="lx0001"):
    return [tuple(r) for r in c.execute(
        "SELECT ticket_id, status FROM trade_tickets WHERE account_id = ? AND journal_ref = ? "
        "AND note LIKE 'EXIT %' ORDER BY rowid", (LIVE, ref)).fetchall()]


def _exit_qty_by_leg(c, ref="lx0001"):
    return {(r[0], float(r[1]), r[2]): r[3] for r in c.execute(
        "SELECT l.side, l.strike, l.option_type, SUM(l.qty_filled) FROM trade_legs l JOIN trade_tickets t "
        "ON t.ticket_id = l.ticket_id WHERE t.account_id = ? AND t.journal_ref = ? AND t.note LIKE 'EXIT %' "
        "GROUP BY l.side, l.strike, l.option_type", (LIVE, ref)).fetchall()}


def _events(c, kind, ref="lx0001"):
    return c.execute("SELECT COUNT(*) FROM paper_account_events WHERE account_id = ? AND journal_ref = ? "
                     "AND event_type = ?", (LIVE, ref, kind)).fetchone()[0]


def _tick(c, minute, chain, epoch, **kw):
    return lp.tick(now=OPEN.replace(minute=minute), conn=c, chain_fn=lambda t, x: chain,
                   sleep_fn=lambda s: None, now_epoch_fn=lambda: float(epoch), interval_s=300, **kw)


class _Raise:
    """The door's sweep dies after the ticket was issued, before any fill."""
    @staticmethod
    def sweep(conn, **kw):
        raise sqlite3.OperationalError("database is locked")


class _NoFill:
    """A venue pass that fills nothing this time."""
    @staticmethod
    def sweep(conn, **kw):
        return {}


def _fail_second_leg_fill(monkeypatch):
    """`database is locked` on the SECOND per-leg apply_fill inside the real
    sweep: leg 0 is FILLED and committed, the basket is cut (audit F05 (a))."""
    real, calls = oms.apply_fill, {"n": 0}

    def flaky(conn, leg_id, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("database is locked")
        return real(conn, leg_id, *a, **k)
    monkeypatch.setattr(oms, "apply_fill", flaky)
    return real


def _cancel_after_a_concurrent_fill(monkeypatch, other_conn):
    """oms.cancel_ticket, but another connection's venue sweep lands first
    (audit F05 (b)): the ticket ends FILLED and the cancel says so."""
    real, seen = oms.cancel_ticket, {}

    def racing(conn, ticket_id, reason="cancelled"):
        pv.sweep(other_conn, stamp=False)
        seen[ticket_id] = real(conn, ticket_id, reason)
        return seen[ticket_id]
    monkeypatch.setattr(oms, "cancel_ticket", racing)
    return seen, real


# ------------------------------------------------------------- F06: one actor per exit

def test_a_stale_open_row_in_a_second_process_issues_no_second_exit(two):
    a, b = two
    ref = _open(a)
    stale_b = lp.open_rows(b, LIVE)[0]                 # process B read the row OPEN
    first = lp._exit(a, lp.open_rows(a, LIVE)[0], PX, "ratchet_hit", OPEN.replace(second=1))
    assert first["status"] == "settled"
    realized = pm.paper_account_summary(a, LIVE)["realized_pnl"]
    second = lp._exit(b, stale_b, PX, "ratchet_hit", OPEN.replace(second=4))
    assert second["status"] == "exit_not_owned" and "'closed'" in second["reason"]
    # B issued and filled NOTHING: one EXIT ticket, one exit venue_fill (+ the entry's)
    assert [s for _, s in _exit_tickets(a, ref)] == [oms.FILLED]
    assert _events(a, "venue_fill", ref) == 2 and _events(a, lp.EVENT_EXIT, ref) == 1
    assert pm.paper_account_summary(a, LIVE)["realized_pnl"] == realized


def test_a_stale_copy_of_a_row_already_exiting_issues_nothing(world):
    c = world
    ref = _open(c)
    row = _row(c)
    assert lp._exit(c, row, PX, "ratchet_hit", OPEN, venue_mod=_Raise)["status"] == "exit_error"
    assert _row(c)["state"] == "exiting" and len(_exit_tickets(c)) == 1
    res = lp._exit(c, dict(row), PX, "ratchet_hit", OPEN.replace(second=1))     # the stale 'open' copy
    assert res["status"] == "exit_not_owned" and "'exiting'" in res["reason"]
    assert len(_exit_tickets(c, ref)) == 1                                       # no second ticket
    assert _row(c)["exit_started_at"] == lp._iso(OPEN)                           # the owner's stamp kept


def test_a_reopen_never_resurrects_a_closed_row(world):
    """F06 (b): a resume holding an `exiting` snapshot, after another actor
    closed the row, used to reopen it — a ghost with its lock released."""
    c = world
    ref = _open(c)
    assert lp._exit(c, _row(c), PX, "pre_expiry_exit", OPEN, venue_mod=_Raise)["status"] == "exit_error"
    snapshot = _row(c)                                     # the resuming actor's read: exiting, ticket PENDING
    settled = lp._settle(c, snapshot, 70.0, "expired", "expiry_backstop", 0.0, OPEN)   # another actor closes it
    assert settled["status"] == "settled" and pm.paper_account_summary(c, LIVE)["open_locks"] == 0
    res = lp._resume_exiting(c, snapshot, OPEN.replace(minute=1))
    assert res["status"] == "not_reopened" and "'closed'" in res["reason"]
    assert _state(c, ref) == ("closed", settled["pnl_net"])
    assert lp.has_open_position(c, LIVE, ref) is False and lp.open_rows(c, LIVE) == []
    assert oms.ticket_status(c, _exit_tickets(c)[0][0]) == oms.CANCELLED       # its own ticket still withdrawn


def test_the_unfilled_branch_never_reopens_a_row_closed_under_it(world):
    c = world
    ref = _open(c)
    row = _row(c)

    class OtherActorClosesIt:                              # e.g. a backstop's _settle on its own copy
        @staticmethod
        def sweep(conn, **kw):
            lp._settle(conn, dict(row), 70.0, "expired", "expiry_backstop", 0.0, OPEN)
            return {}
    res = lp._exit(c, row, PX, "ratchet_hit", OPEN, venue_mod=OtherActorClosesIt)
    assert res["status"] == "not_reopened"
    assert _state(c, ref)[0] == "closed" and lp.has_open_position(c, LIVE, ref) is False
    assert _events(c, lp.EVENT_UNFILLED, ref) == 0                     # no "position kept" claim
    # the withdrawn ticket is not stamped onto the row another actor closed
    assert c.execute("SELECT exit_ticket_id FROM paper_live_positions").fetchone()[0] is None


def test_reopen_is_a_compare_and_set_on_the_attempt_it_undoes(world):
    c = world
    _open(c)
    assert lp._exit(c, _row(c), PX, "ratchet_hit", OPEN, venue_mod=_Raise)["status"] == "exit_error"
    row = _row(c)
    assert lp._reopen(c, row, "tkt:not-this-attempt", row["exit_started_at"]) is False
    assert lp._reopen(c, row, row["exit_ticket_id"], "2026-09-29T10:00:00") is False
    assert _row(c)["state"] == "exiting"
    assert lp._reopen(c, row, row["exit_ticket_id"], row["exit_started_at"]) is True
    assert _row(c)["state"] == "open"
    assert lp._reopen(c, row, row["exit_ticket_id"], row["exit_started_at"]) is False   # not exiting any more


# ------------------------------------------------------------- F05: what filled is what counts

def test_a_partial_basket_re_exits_only_the_open_leg_and_settles_on_both_tickets(world, monkeypatch):
    c = world
    ref = _open(c)
    assert _tick(c, 0, ENTRY, 1000)["marked"] == 1                    # the chain is cached at epoch 1000
    real_fill = _fail_second_leg_fill(monkeypatch)
    first = lp._exit(c, _row(c), PX, "profit_take", OPEN.replace(minute=1))
    monkeypatch.setattr(oms, "apply_fill", real_fill)
    assert first["status"] == "exit_error"
    (t1, st1), = _exit_tickets(c)
    assert st1 == oms.PARTIAL
    legs1 = {(l["side"], float(l["strike"])): l for l in oms.ticket_view(c, t1)["legs"]}
    assert legs1[("BUY", 24200.0)]["state"] == oms.FILLED              # the short was bought back @60
    assert legs1[("SELL", 24000.0)]["state"] == oms.PENDING            # the long was not sold

    # next tick: the resume cancels the cut basket's remainder and KEEPS the row exiting
    t2 = _tick(c, 2, LATER, 1060)                                      # chain not due (60 s < 300 s)
    (res,) = t2["resumes"]
    assert res["status"] == "partial" and res["cancelled"] == {t1: oms.PARTIAL}
    assert res["remaining"] == {"BUY 24000CE": 65}
    r = _row(c)
    assert r["state"] == "exiting" and r["lots"] == 1                  # never restored whole
    assert len(_exit_tickets(c)) == 1 and t2["exits"] == []            # nothing issued on a cached chain
    assert [n["kind"] for n in t2["row_notes"]] == ["partial_exit"]
    assert "not fetched this tick" in t2["row_notes"][0]["reason"]
    assert pm._active_shadow_lock(c, LIVE, ref) is not None and lp.has_open_position(c, LIVE, ref)
    assert _events(c, lp.EVENT_EXIT_PARTIAL) == 1
    from src.live_bridge import LiveTickLog                            # the F02 log line says so
    log = LiveTickLog()
    lines = log.lines(t2, OPEN.replace(minute=2))
    assert any(f"{ref} crashed exit resumed: partial" in l for l in lines)
    assert any(f"{ref} partial_exit: profit_take basket partly filled" in l for l in lines)

    # a standing partial is a (de-duplicated) note, not a resume line every tick
    t3 = _tick(c, 3, LATER, 1120)
    assert t3["resumed"] == 0 and "resumes" not in t3
    assert log.lines(t3, OPEN.replace(minute=3)) == []                 # already said, within 30 min
    assert [n["kind"] for n in t3["row_notes"]] == ["partial_exit"] and len(_exit_tickets(c)) == 1
    assert _events(c, lp.EVENT_EXIT_PARTIAL) == 1                      # one event a day

    # the chain falls due: ONE leg is exited, at the remaining quantity, crossed (long at the bid 120)
    t4 = _tick(c, 6, LATER, 1400)
    (ex,) = t4["exits"]
    assert ex["completion"] is True and ex["status"] == "settled" and ex["signal"] == "profit_take"
    (_, s1), (t2id, s2) = _exit_tickets(c)
    legs2 = oms.ticket_view(c, t2id)["legs"]
    assert [(l["side"], float(l["strike"]), l["qty_target"], l["avg_fill_price"]) for l in legs2] \
        == [("SELL", 24000.0, 65, 120.0)]
    assert _exit_qty_by_leg(c) == {("BUY", 24200.0, "CE"): 65, ("SELL", 24000.0, "CE"): 65}   # none twice
    # P&L on BOTH tickets' real fills: +120 (long sold, ticket 2) - 60 (short bought, ticket 1)
    fills = {(24000.0, "CE"): 120.0, (24200.0, "CE"): 60.0}
    assert ex["exit_mark_ps"] == 60.0 and ex["fills"] == {"24000CE": 120.0, "24200CE": 60.0}
    assert ex["pnl_net"] == round((60.0 - 70.0) * 65 - lp._frictions(r, fills), 2)
    assert ex["exit_tickets"] == [t1, t2id] and ex["ticket_id"] == t2id
    closed = lp.positions(c)[0]
    assert closed["state"] == "closed" and closed["pnl_net"] == ex["pnl_net"]
    assert _events(c, lp.EVENT_EXIT) == 1 and pm._active_shadow_lock(c, LIVE, ref) is None
    assert pm.paper_account_summary(c, LIVE)["realized_pnl"] == ex["pnl_net"]
    assert _tick(c, 12, LATER, 2000)["exits"] == [] and len(_exit_tickets(c)) == 2   # and it stays done


def test_a_basket_the_venue_fills_in_part_is_never_restored_whole(world):
    """The same rule in `_exit`'s own not-filled branch: the venue closed
    one leg, the rest is cancelled, the row stays exiting."""
    c = world
    ref = _open(c)

    class OneLeg:
        @staticmethod
        def sweep(conn, **kw):
            leg = oms.open_legs(conn)[0]
            t = dict(conn.execute("SELECT * FROM trade_tickets WHERE ticket_id = ?", (leg["ticket_id"],)).fetchone())
            pv.fill_leg(conn, leg, t)
            return {}
    res = lp._exit(c, _row(c), PX, "ratchet_hit", OPEN, venue_mod=OneLeg)
    assert res["status"] == "partial" and res["remaining"] == {"BUY 24000CE": 65}
    assert _row(c)["state"] == "exiting" and _events(c, lp.EVENT_UNFILLED, ref) == 0
    (t1, st1), = _exit_tickets(c)
    assert st1 == oms.PARTIAL                                          # its remainder withdrawn
    assert pm._active_shadow_lock(c, LIVE, ref) is not None


def test_a_remainder_with_no_usable_quote_waits_named_and_issues_nothing(world, monkeypatch):
    c = world
    _open(c)
    real_fill = _fail_second_leg_fill(monkeypatch)
    assert lp._exit(c, _row(c), PX, "profit_take", OPEN)["status"] == "exit_error"
    monkeypatch.setattr(oms, "apply_fill", real_fill)
    no_bid = _chain({(24000, "CE"): (130.0, 125.0, 127.0), (24200, "CE"): (40.0, 42.0, 41.0)})   # crossed book
    t = _tick(c, 1, no_bid, 1000)                                      # empty cache: fetched, fresh
    assert t["exits"] == [] and len(_exit_tickets(c)) == 1
    (note,) = t["row_notes"]
    assert note["kind"] == "partial_exit" and "BUY 24000CE: crossed book" in note["reason"]
    assert _row(c)["state"] == "exiting"


def _cut_basket(c, monkeypatch, minute=1):
    """A first exit attempt whose basket is cut between its two per-leg
    commits (the short bought back @60, the long still open), then the
    resume that withdraws the remainder: the row is a standing partial."""
    real_fill = _fail_second_leg_fill(monkeypatch)
    assert lp._exit(c, _row(c), PX, "profit_take", OPEN.replace(minute=minute))["status"] == "exit_error"
    monkeypatch.setattr(oms, "apply_fill", real_fill)
    assert lp._resume_exiting(c, _row(c), OPEN.replace(minute=minute + 1))["status"] == "partial"
    return _exit_tickets(c)[0][0]


def test_a_completion_whose_ticket_id_is_an_earlier_tickets_issues_nothing(world, monkeypatch):
    """Exit ticket ids are keyed to the wall-clock second and issuing is
    idempotent: a completion in the first attempt's second would get the
    first ticket back. It is refused by name, never read as a fill."""
    c = world
    _open(c)
    frozen = _Clock.t + timedelta(seconds=1)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)
    monkeypatch.setattr(sr, "datetime", Frozen)
    t1 = _cut_basket(c, monkeypatch)
    res = lp._complete_exit(c, _row(c), LATER, "2026-09-29T11:03:00", OPEN.replace(minute=3))
    assert res["status"] == "partial_held" and "same wall-clock second" in res["reason"]
    assert _exit_tickets(c) == [(t1, oms.PARTIAL)] and _row(c)["state"] == "exiting"
    _Clock.t = frozen + timedelta(minutes=6)
    monkeypatch.setattr(sr, "datetime", _FakeDT)                      # a later second: it completes
    res = lp._complete_exit(c, _row(c), LATER, "2026-09-29T11:09:00", OPEN.replace(minute=9))
    assert res["status"] == "settled" and len(_exit_tickets(c)) == 2


def test_a_stale_completion_issues_nothing(world, monkeypatch):
    """F06 for the completion too: its claim is a compare-and-set on the
    row as the tick read it."""
    c = world
    _open(c)
    _cut_basket(c, monkeypatch)
    stale = _row(c)                                                   # actor B's read
    res = lp._complete_exit(c, _row(c), LATER, "2026-09-29T11:03:00", OPEN.replace(minute=3), venue_mod=_NoFill)
    assert res["status"] == "partial" and len(_exit_tickets(c)) == 2   # A tried; nothing filled; withdrawn
    assert _row(c)["exit_ticket_id"] == _exit_tickets(c)[1][0]
    res = lp._complete_exit(c, stale, LATER, "2026-09-29T11:04:00", OPEN.replace(minute=4))
    assert res["status"] == "exit_not_owned" and len(_exit_tickets(c)) == 2


def test_a_ticket_filled_under_the_resume_cancel_is_settled_not_reopened(world, monkeypatch):
    c = world
    ref = _open(c)
    assert lp._exit(c, _row(c), PX, "ratchet_hit", OPEN, venue_mod=_Raise)["status"] == "exit_error"
    (t1, st1), = _exit_tickets(c)
    assert st1 == oms.PENDING
    seen, real_cancel = _cancel_after_a_concurrent_fill(monkeypatch, c)
    t = _tick(c, 1, ENTRY, 1000)
    (res,) = t["resumes"]
    assert seen[t1]["status"] == oms.FILLED and res["cancelled"] == {t1: oms.FILLED}
    assert res["status"] == "settled" and res["exit_mark_ps"] == 90.0 and res["resumed"] is True
    assert _state(c, ref)[0] == "closed" and pm._active_shadow_lock(c, LIVE, ref) is None
    monkeypatch.setattr(oms, "cancel_ticket", real_cancel)
    assert _tick(c, 7, ENTRY, 2000)["exits"] == [] and [s for _, s in _exit_tickets(c)] == [oms.FILLED]


def test_an_exit_ticket_filled_before_its_cancel_is_settled_not_reported_unfilled(two, monkeypatch):
    a, b = two
    ref = _open(a)
    _cancel_after_a_concurrent_fill(monkeypatch, b)                    # B's sweep (another process) wins
    res = lp._exit(a, lp.open_rows(a, LIVE)[0], PX, "ratchet_hit", OPEN, venue_mod=_NoFill)
    assert res["status"] == "settled" and res["exit_mark_ps"] == 90.0
    assert _state(a, ref)[0] == "closed" and [s for _, s in _exit_tickets(a)] == [oms.FILLED]
    assert _events(a, lp.EVENT_UNFILLED, ref) == 0 and _events(a, lp.EVENT_EXIT, ref) == 1


def test_nothing_filled_is_cancelled_and_reopened(world):
    c = world
    ref = _open(c)
    res = lp._exit(c, _row(c), PX, "ratchet_hit", OPEN, venue_mod=_NoFill)
    assert res["status"] == "unfilled" and _row(c)["state"] == "open"
    assert [s for _, s in _exit_tickets(c)] == [oms.CANCELLED] and _events(c, lp.EVENT_UNFILLED, ref) == 1
    # the resume path: a door error, nothing filled -> cancelled and reopened
    row = dict(_row(c), last_exit_attempt_ts=None)
    assert lp._exit(c, row, PX, "ratchet_hit", OPEN.replace(minute=10), venue_mod=_Raise)["status"] == "exit_error"
    t = _tick(c, 11, ENTRY, 1000)
    (r,) = t["resumes"]
    t2 = _exit_tickets(c)[1][0]
    assert r["status"] == "reopened" and r["cancelled"] == {t2: oms.CANCELLED}
    assert _row(c)["state"] == "open" and pm._active_shadow_lock(c, LIVE, ref) is not None


# ------------------------------------------------------------- F06: one tick at a time on the host

def test_a_second_process_skips_its_tick_while_the_lock_is_held(world, tmp_path):
    c = world
    _open(c)
    path = tmp_path / ".live_pricer_tick.lock"
    held, why = lp._tick_lock(path)                     # the other process, mid-tick
    assert held is not None and why is None
    t = _tick(c, 1, ENTRY, 1000, tick_lock=path)
    assert t["marked"] == 0 and t["fetched"] == 0
    assert t["skipped"].startswith("another process is ticking the live account") and str(path) in t["skipped"]
    lp._release_tick_lock(held)
    t = _tick(c, 2, ENTRY, 1000, tick_lock=path)
    assert t["skipped"] is None and t["marked"] == 1
    again, why = lp._tick_lock(path)                     # the tick released it on its way out
    assert again is not None and why is None
    lp._release_tick_lock(again)


def test_the_production_lock_file_is_never_opened_under_pytest(monkeypatch):
    monkeypatch.setattr(lp, "_is_test_env", lambda: False)   # as a test reaching the real chain door does
    assert lp._default_tick_lock() is None
    assert lp._tick_lock(None) == (None, None)
    assert lp.TICK_LOCK_FILE.name == ".live_pricer_tick.lock" and lp.TICK_LOCK_FILE.parent.name == "data"
