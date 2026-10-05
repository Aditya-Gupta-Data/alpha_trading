"""
Audit Chunk 2 F02 + F21 — PAPER_2L_LIVE's quiet states are recorded.

F02: abstentions, unconfirmed cached predicates and held exits used to be
bare counters (or nothing) in `live_pricer.tick`'s summary, which
`live_bridge.live_cycle` threw away — an arm abstaining all week through
its pre-expiry window looked exactly like a healthy one, and the dashboard
kept adding its days-old mark as live MTM. Now:
  1. live_cycle prints the non-routine part of the summary (one header +
     one line per row, ref + reason); a routine all-marked tick prints
     nothing;
  2. a standing state prints once per (row, reason) per 30 minutes, never
     a line per 60-s tick; one-off outcomes (settled / unfilled / error)
     always print;
  3. a held exit and an abstention inside the forced-exit window write ONE
     paper_account_events row per position per day (no Discord card);
  4. the dashboard exposes the live row's last_mark_ts and mark_stale,
     judged in market time as of when the copy being read was taken (the
     box reads a 15-min mirror — review of Fix D).
F21: run_tracker prints every eod_sweep row that errored or is waiting for
bars, with its ref and reason.

Hermetic: the `world` fixture of tests/test_live_account.py (sqlite
':memory:', chains injected, the clock injected); the dashboard reads a
tmp_path database through its own read-only connection.
"""
import os
import sqlite3
from datetime import date, datetime, timezone

import pytest

from src import brain_map, live_bridge as lb, plan_tracker as pt, portfolio_manager as pm
from src.dashboard import data as dash
from src.execution import live_pricer as lp
from tests.test_live_account import LIVE, OPEN, _chain, _open, world  # noqa: F401

KEY = ("NIFTY 50", "2026-10-28")              # _bull_call: BUY 24000 CE / SELL 24200 CE, d 70, width 200
GOOD = _chain({(24000, "CE"): (120.0, 122.0, 121.0), (24200, "CE"): (33.0, 35.0, 34.0)})    # +15 = 11.5%: hold
NO_ASK = _chain({(24000, "CE"): (120.0, 122.0, 121.0), (24200, "CE"): (33.0, None, 34.0)})
OFF_A = _chain({(24000, "CE"): (290.0, 300.0, 295.0), (24200, "CE"): (95.0, 230.0, 60.0)})  # deep ITM, stale last
OFF_B = _chain({(24000, "CE"): (291.0, 301.0, 295.0), (24200, "CE"): (96.0, 231.0, 60.0)})  # same state, re-priced
CROSSED = _chain({(24000, "CE"): (125.0, 122.0, 121.0), (24200, "CE"): (33.0, 35.0, 34.0)})
# the long has lost its bid: crossed exit = 0 - 6 = -6 -> a loss of 76/share > max loss 70 -> HELD
DEAD = _chain({(24000, "CE"): (None, 4.0, None), (24200, "CE"): (5.0, 6.0, 5.5)})


def _tick(c, now, chain, epoch, **kw):
    fn = chain if callable(chain) else (lambda t, x: chain)
    return lp.tick(now=now, conn=c, chain_fn=fn, sleep_fn=lambda s: None, now_epoch_fn=lambda: epoch,
                   interval_s=300, **kw)


def _cycle(c, now, chain, epoch, log):
    """The REAL live_cycle -> live_pricer.tick path (only Dhan is faked)."""
    return lb.live_cycle(("NIFTY 50",), quote_fn=lambda u: None, entries=[], now_fn=lambda: now,
                         live_account_fn=lambda n: _tick(c, n, chain, epoch), live_log=log)


def _events(c, kind=None):
    sql = "SELECT event_type, journal_ref, ts, detail FROM paper_account_events WHERE account_id = ? " \
          "AND event_type IN ('live_exit_held', 'live_mark_abstained')"
    rows = [tuple(r) for r in c.execute(sql + " ORDER BY rowid", (LIVE,)).fetchall()]
    return [r for r in rows if kind is None or r[0] == kind]


@pytest.fixture
def ist_clock(monkeypatch):
    """pm._now_iso (the stamp paper_log_event writes, and the day the
    once-a-day check reads) follows the test's tick clock."""
    clock = {"now": OPEN}
    monkeypatch.setattr(pm, "_now_iso", lambda: clock["now"].isoformat(timespec="seconds"))
    return clock


# ------------------------------------------------------------- 1. the log line

def test_a_routine_all_marked_tick_prints_nothing(world, capsys):
    _open(world)
    log = lb.LiveTickLog()
    _cycle(world, OPEN, GOOD, 1000.0, log)
    _cycle(world, OPEN.replace(minute=1), GOOD, 1060.0, log)
    assert capsys.readouterr().out == ""
    assert lp.open_rows(world, LIVE)[0]["last_mark_ts"] == "2026-09-29T11:01:00"


def test_an_abstaining_tick_prints_one_header_and_the_row_with_its_reason(world, capsys):
    _open(world)
    _cycle(world, OPEN, NO_ASK, 1000.0, lb.LiveTickLog())
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["[Live Bridge] live account tick: 1 open, 0 marked, 1 abstained.",
                     "  (live account: lv0001 abstained: SELL 24200CE: no ask to buy the short leg back; "
                     "last mark never)"]


def test_a_standing_state_prints_once_per_30_minutes_never_per_tick(world, capsys):
    _open(world)
    log = lb.LiveTickLog()
    _cycle(world, OPEN, GOOD, 1000.0, log)                       # a healthy first mark at 11:00
    # 40 one-minute ticks: 11:01-11:04 still mark on the 11:00 chain (routine, silent); from
    # 11:05 the chain is re-fetched every 5 minutes, re-priced each time (230 vs 231 on a
    # stale last of 60) — one standing state, not 36 new ones
    for m in range(1, 41):
        _cycle(world, OPEN.replace(minute=m), OFF_A if m % 2 else OFF_B, 1000.0 + 60 * m, log)
    out = capsys.readouterr().out
    rows = [ln for ln in out.splitlines() if "lv0001 abstained" in ln]
    assert len(rows) == 2 and out.count("[Live Bridge] live account tick:") == 2      # 11:05 and 11:35
    assert rows[0] == ("  (live account: lv0001 abstained: SELL 24200CE: quote 230 is >50% off last 60; "
                       "last mark 2026-09-29T11:04:00)")
    assert "quote 230" in rows[1] or "quote 231" in rows[1]
    # a DIFFERENT reason for the same row is news: printed at once
    lp.reset_cache()
    _cycle(world, OPEN.replace(minute=42), CROSSED, 4000.0, log)
    assert "lv0001 abstained: BUY 24000CE: crossed book (bid > ask)" in capsys.readouterr().out


def test_one_off_outcomes_always_print_and_holds_and_skips_are_deduplicated():
    log = lb.LiveTickLog()
    t0 = datetime(2026, 10, 27, 10, 0)
    settled = {"rows": 1, "marked": 1, "abstained": 0, "skipped": None, "row_notes": [],
               "exits": [{"account_id": LIVE, "journal_ref": "lvS", "signal": "ratchet_hit", "status": "settled",
                          "capture_pct": -4.6, "pnl_net": -486.2, "basis": "live_bid_ask"}]}
    for m in (0, 1):                                    # a one-off outcome is never swallowed
        lines = log.lines(settled, t0.replace(minute=m))
        assert lines[0] == "[Live Bridge] live account tick: 1 open, 1 marked, 1 exit outcome(s)."
        assert lines[1] == ("  (live account: lvS exit SETTLED (ratchet_hit): -5% capture, "
                            "P&L Rs.-486.20 net (live_bid_ask))")
    for status, extra, text in (("unfilled", {"ticket_id": "T1", "reason": "REJECTED"}, "NOT FILLED (ticket T1"),
                                ("exit_error", {"reason": "venue down"}, "door ERROR (venue down)")):
        s = dict(settled, exits=[{"journal_ref": "lvU", "signal": "pre_expiry_exit", "status": status, **extra}])
        assert text in log.lines(s, t0)[1] and text in log.lines(s, t0.replace(minute=1))[1]
    held = dict(settled, marked=1, exits=[{"account_id": LIVE, "journal_ref": "lvH", "signal": "pre_expiry_exit",
                                           "status": "held_loss_beyond_max", "would_loss_ps": 76.0,
                                           "max_loss_ps": 70.0}])
    first = log.lines(held, t0)
    assert first[1] == ("  (live account: lvH pre_expiry_exit exit HELD: a crossed exit would lose 76/share, "
                        "beyond the structure's max loss 70 — position kept)")
    assert log.lines(held, t0.replace(minute=29)) == []                       # inside 30 minutes: silent
    assert len(log.lines(held, t0.replace(minute=30))) == 2                   # the window has passed
    wait = dict(held, exits=[dict(held["exits"][0], status="held_recent_attempt", last="2026-10-27T10:31:00")])
    assert "exit waits" in log.lines(wait, t0.replace(minute=31))[1]
    assert log.lines(wait, t0.replace(minute=32)) == []
    # a tick-level skip prints (deduplicated); a closed market is routine
    boom = {"rows": 0, "marked": 0, "abstained": 0, "exits": [], "skipped": "database is locked"}
    assert log.lines(boom, t0)[1] == "  (live account: tick skipped: database is locked)"
    assert log.lines(boom, t0.replace(minute=1)) == []
    assert log.lines(dict(boom, skipped="market closed"), t0) == []
    # crash resumes and repairs are one-off news
    fixed = {"rows": 1, "marked": 1, "abstained": 0, "exits": [], "skipped": None, "resumed": 1,
             "resumes": [{"journal_ref": "lvR", "status": "reopened"}], "repaired": ["lvP"],
             "late_released": ["lvL"]}
    text = "\n".join(log.lines(fixed, t0))
    assert "1 resumed" in text and "lvR crashed exit resumed: reopened" in text
    assert "lvP position row rebuilt" in text and "lvL lock released late" in text
    assert log.lines(None, t0) == [] and log.lines({"rows": 2, "marked": 2, "exits": []}, t0) == []


def test_live_cycle_no_longer_discards_the_summary_and_a_broken_tick_stays_fail_open(capsys):
    summary = {"rows": 1, "marked": 0, "abstained": 1, "unconfirmed": 1, "skipped": None,
               "exits": [{"journal_ref": "obs1", "signal": "pre_expiry_exit", "status": "exit_error",
                          "reason": "RuntimeError: venue down"}]}
    now = datetime(2026, 10, 27, 10, 0)
    fired = lb.live_cycle(("NIFTY 50",), quote_fn=lambda u: None, entries=[], now_fn=lambda: now,
                          live_account_fn=lambda n: summary, live_log=lb.LiveTickLog())
    out = capsys.readouterr().out
    assert fired == []
    assert "[Live Bridge] live account tick: 1 open, 0 marked, 1 abstained, 1 unconfirmed, 1 exit outcome(s)." in out
    assert "obs1 pre_expiry_exit exit door ERROR (RuntimeError: venue down)" in out
    assert "1 abstained / 1 unconfirmed (no per-row detail in the summary)" in out
    # a raising tick and an unprintable summary both leave the cycle running
    fired = lb.live_cycle(("NIFTY 50",), quote_fn=lambda u: None, entries=[], now_fn=lambda: now,
                          live_account_fn=lambda n: (_ for _ in ()).throw(RuntimeError("boom")),
                          live_log=lb.LiveTickLog())
    assert fired == [] and "(live account tick skipped: boom)" in capsys.readouterr().out

    class Broken(lb.LiveTickLog):
        def lines(self, summary, now):
            raise ValueError("bad shape")
    assert lb.live_cycle(("NIFTY 50",), quote_fn=lambda u: None, entries=[], now_fn=lambda: now,
                         live_account_fn=lambda n: summary, live_log=Broken()) == []
    assert "(live account tick summary not printed: bad shape)" in capsys.readouterr().out


def test_unconfirmed_cached_predicates_are_named_with_why_they_could_not_be_reverified(world):
    _open(world)
    in_window = datetime(2026, 10, 27, 15, 28)               # 1 day to expiry -> pre_expiry_exit fires
    lp._CHAIN_CACHE[KEY] = (1000.0, "2026-10-27T15:20:00", GOOD)
    t = _tick(world, in_window, GOOD, 1100.0)                 # cached, and past the 15:27 fetch cutoff
    assert t["unconfirmed"] == 1 and t["exits"] == []
    note = t["row_notes"][0]
    assert (note["journal_ref"], note["kind"], note["days_left"]) == ("lv0001", "unconfirmed", 1)
    assert note["reason"] == ("pre_expiry_exit fired on a cached chain (2026-10-27T15:20:00) and past the "
                              "fetch cutoff — held for the next tick's fresh chain")
    t = _tick(world, in_window.replace(hour=11), GOOD, 1150.0, max_fetches=0)
    assert "the per-tick fetch cap or time budget was reached" in t["row_notes"][0]["reason"]
    t = _tick(world, in_window.replace(hour=11), lambda tk, x: (_ for _ in ()).throw(RuntimeError("dhan")),
              1200.0)
    assert "the re-verify fetch failed" in t["row_notes"][0]["reason"]
    assert lp.open_rows(world, LIVE)[0]["state"] == "open"


def test_every_abstention_names_its_row_reason_and_window(world):
    _open(world)
    t = _tick(world, OPEN, lambda tk, x: (_ for _ in ()).throw(RuntimeError("dhan down")), 1000.0)
    assert t["abstained"] == 1 and t["row_notes"] == [
        {"account_id": LIVE, "journal_ref": "lv0001", "kind": "abstained",
         "reason": "no chain: the fetch failed this tick", "days_left": 29, "in_exit_window": False,
         "last_mark_ts": None}]
    t = _tick(world, OPEN, NO_ASK, 1000.0, max_fetches=0)
    assert t["row_notes"][0]["reason"] == "no chain yet: the per-tick fetch cap or time budget was reached"
    t = _tick(world, OPEN.replace(hour=15, minute=28), NO_ASK, 1000.0)
    assert t["row_notes"][0]["reason"] == "no chain yet: past the fetch cutoff"


# ------------------------------------------------------------- 3. the audit events

def test_a_held_exit_writes_one_live_exit_held_event_per_row_per_day_and_no_card(world, ist_clock, monkeypatch):
    from src import notifier
    cards = []
    monkeypatch.setattr(notifier, "fire_broadcast", lambda p: cards.append(p))
    monkeypatch.setattr(notifier, "send_discord_message", lambda *a, **k: cards.append(a))
    _open(world)
    for m in range(10):                                        # ten 60-s ticks in the pre-expiry window
        now = datetime(2026, 10, 27, 10, m)
        ist_clock["now"] = now
        t = _tick(world, now, DEAD, 3_000_000.0 + 60 * m)
        assert t["exits"][0]["status"] == "held_loss_beyond_max" and t["exits"][0]["account_id"] == LIVE
    ev = _events(world, "live_exit_held")
    assert len(ev) == 1 and ev[0][1] == "lv0001" and ev[0][2] == "2026-10-27T10:00:00"
    assert ev[0][3] == ("pre_expiry_exit held: a crossed exit would lose 76/share, beyond the structure's max "
                        "loss 70 — position kept; the expiry backstop can never do worse")
    ist_clock["now"] = now = datetime(2026, 10, 28, 9, 20)     # the next day: one more row
    _tick(world, now, DEAD, 3_100_000.0)
    assert [e[2][:10] for e in _events(world, "live_exit_held")] == ["2026-10-27", "2026-10-28"]
    assert cards == [] and lp.open_rows(world, LIVE)[0]["state"] == "open"


def test_an_abstention_inside_the_forced_exit_window_writes_one_event_per_day_outside_none(world, ist_clock):
    _open(world)
    _tick(world, OPEN, GOOD, 1000.0)                           # last mark 2026-09-29T11:00:00
    for d in (22, 23):                                         # 6 and 5 days left: outside the 2-day window
        ist_clock["now"] = now = datetime(2026, 10, d, 11, 0)
        t = _tick(world, now, NO_ASK, 10_000.0 * d)
        assert t["abstained"] == 1 and t["row_notes"][0]["in_exit_window"] is False
    assert _events(world) == []
    for d in (26, 27):                                         # inside it: one row a day, however many ticks
        for h in (10, 12, 14):
            ist_clock["now"] = now = datetime(2026, 10, d, h, 0)
            t = _tick(world, now, NO_ASK, 10_000.0 * d + 1_000.0 * h)
            assert t["row_notes"][0]["in_exit_window"] is True
    ev = _events(world, "live_mark_abstained")
    assert [(e[1], e[2]) for e in ev] == [("lv0001", "2026-10-26T10:00:00"), ("lv0001", "2026-10-27T10:00:00")]
    assert ev[0][3] == ("no usable mark inside the forced-exit window (2d to expiry 2026-10-28): SELL 24200CE: "
                        "no ask to buy the short leg back — the pre-expiry exit cannot fire on this chain; "
                        "last mark 2026-09-29T11:00:00; the expiry backstop still covers it")
    # an abstention for want of any chain counts too
    lp.reset_cache()
    ist_clock["now"] = now = datetime(2026, 10, 28, 10, 0)
    _tick(world, now, lambda tk, x: (_ for _ in ()).throw(RuntimeError("dhan down")), 900_000.0)
    assert "no chain: the fetch failed this tick" in _events(world, "live_mark_abstained")[-1][3]


def test_a_failed_event_write_is_swallowed_and_the_next_row_is_still_processed(world, ist_clock, monkeypatch,
                                                                              capsys):
    _open(world, "lv0001")
    _open(world, "lv0002")
    monkeypatch.setattr(pm, "paper_log_event",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    ist_clock["now"] = now = datetime(2026, 10, 27, 10, 0)
    t = _tick(world, now, DEAD, 3_000_000.0)
    assert [x["status"] for x in t["exits"]] == ["held_loss_beyond_max"] * 2 and t["marked"] == 2
    out = capsys.readouterr().out
    assert out.count("live_exit_held event for lv000") == 2 and "database is locked" in out
    assert all(r["last_mark_ts"] == "2026-10-27T10:00:00" for r in lp.open_rows(world, LIVE))


def test_the_repro_an_unmarkable_structure_in_its_window_is_now_visible_end_to_end(world, ist_clock, capsys):
    """test_illiq_silent_stall's scenario through the real live_cycle: it
    used to leave no line, no event and a fresh-looking dashboard mark."""
    _open(world, "st01")
    log = lb.LiveTickLog()
    _cycle(world, OPEN, GOOD, 1000.0, log)
    capsys.readouterr()
    epoch = 2000.0
    for d in (26, 27):
        for minute in range(0, 60):                            # an hour of 60-s ticks each day
            ist_clock["now"] = now = datetime(2026, 10, d, 10, minute)
            epoch += 60.0
            _cycle(world, now, OFF_A, epoch, log)
    out = capsys.readouterr().out
    assert out.count("st01 abstained [forced-exit window, 2d to expiry]") == 2          # 10:00, 10:30 on 10-26
    assert out.count("st01 abstained [forced-exit window, 1d to expiry]") == 2          # 10:00, 10:30 on 10-27
    assert [e[2][:10] for e in _events(world, "live_mark_abstained")] == ["2026-10-26", "2026-10-27"]
    u = dash.unrealized_by_account(world, snapshot={}, rows=[], now=datetime(2026, 10, 27, 11, 0))
    assert u[LIVE]["last_mark_ts"] == "2026-09-29T11:00:00" and u[LIVE]["mark_stale"] is True
    assert u[LIVE]["unrealized_pnl"] == pytest.approx(15.0 * 65)                        # shown, but flagged


# the ROW in both de-dup keys (review of Fix D): the arm normally holds
# several positions; one row's line or day-event must never silence another's

def test_two_rows_abstaining_for_the_same_reason_are_both_printed_and_both_get_an_event(world, ist_clock,
                                                                                       capsys):
    _open(world, "lvA")
    _open(world, "lvB")
    ist_clock["now"] = now = datetime(2026, 10, 27, 10, 0)     # 1 day left: inside the forced-exit window
    _cycle(world, now, NO_ASK, 3_000_000.0, lb.LiveTickLog())
    out = capsys.readouterr().out
    assert "lvA abstained" in out and "lvB abstained" in out
    assert sorted(e[1] for e in _events(world, "live_mark_abstained")) == ["lvA", "lvB"]


def test_two_held_rows_are_both_printed_and_both_get_an_event(world, ist_clock, capsys):
    _open(world, "lvA")
    _open(world, "lvB")
    ist_clock["now"] = now = datetime(2026, 10, 27, 10, 0)
    _cycle(world, now, DEAD, 3_000_000.0, lb.LiveTickLog())
    out = capsys.readouterr().out
    assert "lvA pre_expiry_exit exit HELD" in out and "lvB pre_expiry_exit exit HELD" in out
    assert sorted(e[1] for e in _events(world, "live_exit_held")) == ["lvA", "lvB"]


# ------------------------------------------------------------- 4. the dashboard

def _live_db(tmp_path, rows, name="bm.db"):
    """A brain_map file holding PAPER_2L_LIVE rows: (last_mark_ts, quote_ts, opened_at, last_profit_ps)."""
    p = tmp_path / name
    c = brain_map.connect(str(p))
    pm.get_account(c)
    lp.ensure_schema(c)
    for i, (mark_ts, quote_ts, opened, profit) in enumerate(rows):
        c.execute("INSERT INTO paper_live_positions (account_id, journal_ref, ticker, expiry, lots, lot_size, "
                  "legs_json, entry_mark_ps, width_ps, max_profit_ps, max_loss_ps, opened_at, last_mark_ts, "
                  "quote_ts, last_profit_ps, state) VALUES (?, ?, 'NIFTY 50', '2026-10-28', 1, 65, '[]', 70, 200, "
                  "130, 70, ?, ?, ?, ?, 'open')", (LIVE, f"lv{i}", opened, mark_ts, quote_ts, profit))
    c.commit()
    c.close()
    return p


@pytest.mark.parametrize("now, mark_ts, quote_ts, stale", [
    (datetime(2026, 10, 27, 11, 0), "2026-10-27T10:55:00", "2026-10-27T10:52:00", False),   # in session, fresh
    (datetime(2026, 10, 27, 11, 0), "2026-10-27T10:49:00", "2026-10-27T10:49:00", True),    # 11 min > 2 x 300 s
    (datetime(2026, 10, 27, 11, 0), "2026-10-27T10:59:00", "2026-10-27T10:30:00", True),    # fresh mark, old chain (F19)
    (datetime(2026, 10, 27, 20, 0), "2026-10-27T15:29:00", "2026-10-27T15:24:00", False),   # evening: as of 15:30
    (datetime(2026, 10, 27, 20, 0), "2026-10-27T11:00:00", "2026-10-27T11:00:00", True),    # stopped marking at 11
    (datetime(2026, 10, 28, 8, 0), "2026-10-27T15:29:00", "2026-10-27T15:25:00", False),    # pre-open: last close
    (datetime(2026, 10, 24, 12, 0), "2026-10-23T15:28:00", "2026-10-23T15:22:00", False),   # Saturday -> Fri close
    (datetime(2026, 10, 20, 12, 0), "2026-10-19T15:28:00", "2026-10-19T15:22:00", False),   # Dussehra -> Mon close
    (datetime(2026, 10, 21, 9, 30), "2026-10-19T15:28:00", "2026-10-19T15:22:00", True),    # next session open
    (datetime(2026, 10, 27, 9, 20), "2026-10-26T15:29:00", "2026-10-26T15:26:00", False),   # 9 market-minutes
])
def test_the_live_mark_carries_its_age_and_a_stale_flag(tmp_path, now, mark_ts, quote_ts, stale):
    p = _live_db(tmp_path, [(mark_ts, quote_ts, "2026-10-06T10:00:00", 15.0)])
    conn = dash.connect_ro(p)                                  # read-only: the dashboard never writes
    u = dash.unrealized_by_account(conn, snapshot={}, rows=[], now=now)
    conn.close()
    assert u[LIVE]["last_mark_ts"] == mark_ts and u[LIVE]["mark_stale"] is stale
    assert u[LIVE]["unrealized_pnl"] == 975.0 and u[LIVE]["marked_positions"] == 1


def test_never_marked_rows_age_from_their_open_and_the_oldest_mark_is_the_accounts(tmp_path):
    now = datetime(2026, 10, 27, 11, 0)
    p = _live_db(tmp_path, [("2026-10-27T10:58:00", "2026-10-27T10:57:00", "2026-10-06T10:00:00", 15.0),
                            ("2026-10-27T10:20:00", "2026-10-27T10:20:00", "2026-10-06T10:00:00", -5.0)])
    conn = dash.connect_ro(p)
    u = dash.unrealized_by_account(conn, snapshot={}, rows=[], now=now)
    conn.close()
    assert u[LIVE]["last_mark_ts"] == "2026-10-27T10:20:00" and u[LIVE]["mark_stale"] is True
    assert u[LIVE]["unrealized_pnl"] == 650.0 and (u[LIVE]["marked_positions"], u[LIVE]["open_positions"]) == (2, 2)
    # a position opened 5 minutes ago and not yet marked is not stale; one opened 30 minutes ago is
    for opened, stale in (("2026-10-27T10:55:00", False), ("2026-10-27T10:30:00", True)):
        conn = dash.connect_ro(_live_db(tmp_path, [(None, None, opened, None)], name=f"{opened[-5:-3]}.db"))
        u = dash.unrealized_by_account(conn, snapshot={}, rows=[], now=now)
        conn.close()
        assert u[LIVE]["mark_stale"] is stale and u[LIVE]["last_mark_ts"] is None
        assert u[LIVE]["unrealized_pnl"] is None and u[LIVE]["open_positions"] == 1


def test_an_unknown_quote_interval_or_unparseable_stamp_is_unknown_not_fresh(tmp_path, monkeypatch):
    now = datetime(2026, 10, 27, 11, 0)
    p = _live_db(tmp_path, [("not-a-time", "also-not", "2026-10-06T10:00:00", 15.0)])
    conn = dash.connect_ro(p)
    assert dash.unrealized_by_account(conn, snapshot={}, rows=[], now=now)[LIVE]["mark_stale"] is None
    conn.close()
    import src.config as cfg
    monkeypatch.delattr(cfg, "LIVE_QUOTE_INTERVAL_SECONDS")
    conn = dash.connect_ro(_live_db(tmp_path, [("2026-10-27T10:58:00", "2026-10-27T10:58:00",
                                                "2026-10-06T10:00:00", 15.0)], name="fresh.db"))
    assert dash.unrealized_by_account(conn, snapshot={}, rows=[], now=now)[LIVE]["mark_stale"] is None
    conn.close()


def test_treasury_dates_the_live_arms_marks_by_its_own_mark_not_the_snapshot(tmp_path, monkeypatch):
    import json
    p = _live_db(tmp_path, [("2026-09-29T11:00:00", "2026-09-29T10:58:00", "2026-09-29T10:00:00", 15.0)])
    c = brain_map.connect(str(p))
    pm.request_entry(c, "lv0", 17550.0)
    pm.paper_request_entry(c, LIVE, "lv0", 17550.0, lots=1, primary_lots=1)
    c.close()
    snap = tmp_path / "market_snapshot.json"
    snap.write_text(json.dumps({"as_of": "2026-10-27T15:29:00+05:30", "marks": []}))
    monkeypatch.setattr(dash, "SNAPSHOT_PATH", snap)
    T = dash.treasury(p)
    assert T["PAPER_10L"]["marks_as_of"] == "2026-10-27T15:29:00+05:30"     # the primary: the snapshot
    a = T[LIVE]
    assert a["marks_as_of"] == "2026-09-29T11:00:00" and a["last_mark_ts"] == "2026-09-29T11:00:00"
    assert a["mark_stale"] is True and a["unrealized_pnl"] == 975.0          # a week-old mark, flagged


# review of Fix D: the deployed page reads a brain_map COPY pushed every 15
# minutes (cron #33, the Oracle box) — the mark is judged as of the copy

def _as_captured(p, t: datetime):
    """Stamp the copy's mtime the way rsync -a lands cron #33's push on the box."""
    ts = t.replace(tzinfo=dash.IST).timestamp()
    os.utime(p, (ts, ts))
    return p


def _mirror(tmp_path, monkeypatch, mark_ts, quote_ts, captured: datetime):
    import json
    p = _live_db(tmp_path, [(mark_ts, quote_ts, "2026-10-06T10:00:00", 15.0)])
    c = brain_map.connect(str(p))
    pm.request_entry(c, "lv0", 17550.0)
    pm.paper_request_entry(c, LIVE, "lv0", 17550.0, lots=1, primary_lots=1)
    c.close()
    snap = tmp_path / "market_snapshot.json"
    snap.write_text(json.dumps({"as_of": f"{mark_ts}+05:30", "marks": []}))
    monkeypatch.setattr(dash, "SNAPSHOT_PATH", snap)
    return _as_captured(p, captured)


def test_a_healthy_arm_read_from_the_15_min_mirror_is_never_flagged_between_pushes(tmp_path, monkeypatch):
    """The 10:15 push carries a healthy mark (ticked 10:14:30 on the 10:11:40
    chain, the normal 5-min refresh). Judged on the viewer's clock it went
    'stale' from ~10:22 to the next push — 8 of every 15 minutes."""
    p = _mirror(tmp_path, monkeypatch, "2026-10-27T10:14:30", "2026-10-27T10:11:40",
                captured=datetime(2026, 10, 27, 10, 15, 2))
    for m in range(15, 30):
        a = dash.treasury(p, now=datetime(2026, 10, 27, 10, m, 30))[LIVE]
        assert a["mark_stale"] is False, f"10:{m}:30"
        assert a["mark_stale_as_of"] == "2026-10-27T10:15:02" and a["mark_stale_after_s"] == 600.0


def test_a_loop_that_stopped_is_still_flagged_because_the_pushes_go_on(tmp_path, monkeypatch):
    p = _mirror(tmp_path, monkeypatch, "2026-10-27T10:59:30", "2026-10-27T10:58:00",
                captured=datetime(2026, 10, 27, 11, 0, 2))
    assert dash.treasury(p, now=datetime(2026, 10, 27, 11, 14))[LIVE]["mark_stale"] is False
    _as_captured(p, datetime(2026, 10, 27, 11, 15, 2))          # the next push: the same, unmoved marks
    a = dash.treasury(p, now=datetime(2026, 10, 27, 11, 16))[LIVE]
    assert a["mark_stale"] is True and a["mark_stale_as_of"] == "2026-10-27T11:15:02"


def test_the_opens_first_push_is_not_flagged_for_the_overnight_gap(tmp_path, monkeypatch):
    # the 09:15 push lands before the arm's first tick: yesterday's 15:29:30 mark on the 15:25 chain
    p = _mirror(tmp_path, monkeypatch, "2026-10-26T15:29:30", "2026-10-26T15:25:00",
                captured=datetime(2026, 10, 27, 9, 15, 5))
    assert dash.treasury(p, now=datetime(2026, 10, 27, 9, 29))[LIVE]["mark_stale"] is False
    _as_captured(p, datetime(2026, 10, 27, 9, 30, 2))           # 15 market-minutes on, still yesterday's mark
    assert dash.treasury(p, now=datetime(2026, 10, 27, 9, 31))[LIVE]["mark_stale"] is True


def test_the_reference_is_the_earlier_of_now_and_the_capture_in_ist(tmp_path):
    p = _live_db(tmp_path, [("2026-10-27T10:49:00", "2026-10-27T10:49:00", "2026-10-06T10:00:00", 15.0)])
    conn = dash.connect_ro(p)
    utc = timezone.utc

    def u(**kw):
        return dash.unrealized_by_account(conn, snapshot={}, rows=[], **kw)[LIVE]

    a = u(now=datetime(2026, 10, 27, 5, 30, tzinfo=utc))              # 11:00 IST: 11 market-minutes old
    assert a["mark_stale"] is True and a["mark_stale_as_of"] == "2026-10-27T11:00:00"
    a = u(now=datetime(2026, 10, 27, 11, 0), captured_at=datetime(2026, 10, 27, 5, 25, tzinfo=utc))
    assert a["mark_stale"] is False and a["mark_stale_as_of"] == "2026-10-27T10:55:00"   # 6 min at capture
    # a copy stamped after the viewer's clock (box behind the VM) never moves the reference forward
    a = u(now=datetime(2026, 10, 27, 10, 55), captured_at=datetime(2026, 10, 27, 11, 30))
    assert a["mark_stale_as_of"] == "2026-10-27T10:55:00"
    conn.close()
    assert dash._captured_at(tmp_path / "absent.db") is None           # unknown -> the viewer's clock


@pytest.fixture
def utc_process(monkeypatch):
    """The box (and the VM) may run in UTC; the engine stamps naive IST."""
    import time
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_a_copys_mtime_is_read_in_ist_whatever_the_box_timezone(tmp_path, utc_process):
    p = _as_captured(_live_db(tmp_path, []), datetime(2026, 10, 27, 10, 15, 2))
    assert dash._captured_at(p) == datetime(2026, 10, 27, 10, 15, 2)


# ------------------------------------------------------------- F21: the expiry backstop's waits and errors

def _run_tracker_with(monkeypatch, sweep):
    monkeypatch.setattr(lp, "eod_sweep_standalone", lambda today=None: sweep())
    monkeypatch.setattr(pt.journal, "read_all", lambda: [])
    return pt.run_tracker(email=False)


def test_run_tracker_names_every_failed_settlement_and_every_row_waiting_for_bars(world, monkeypatch, capsys):
    c = world
    _open(c, "lvE")
    bars = [("2026-10-28", 24000.0, 24400.0, 24350.0)]
    real_release = pm.paper_release_rows
    monkeypatch.setattr(pm, "paper_release_rows",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    sweep = lambda: lp.eod_sweep(c, today=date(2026, 10, 29), bars_fn=lambda t, s: bars,
                                 now=datetime(2026, 10, 29, 9, 35))
    for _hour in range(3):                                     # the hourly Auto-Sync, failing each time
        assert _run_tracker_with(monkeypatch, sweep) == 0
        out = capsys.readouterr().out
        assert ("Plan tracker: live account expiry settlement FAILED — lvE: database is locked; the row stays "
                "open with its lock and is retried next run.") in out
    assert lp.open_rows(c, LIVE)[0]["state"] == "open"
    monkeypatch.setattr(pm, "paper_release_rows", real_release)
    _run_tracker_with(monkeypatch, sweep)
    out = capsys.readouterr().out
    assert "live account settled 1 expired position(s)" in out and "FAILED" not in out
    # a row inside the grace window with no bars is named with its reason
    _open(c, "lvW")
    r = lp.eod_sweep(c, today=date(2026, 10, 29), bars_fn=lambda t, s: [], now=datetime(2026, 10, 29, 9, 35))
    assert r["waiting"] == ["lvW"] and r["settled"] == [] and r["errors"] == []
    assert r["reasons"]["lvW"] == ("NIFTY 50 expired 2026-10-28 and no daily close on or before expiry has "
                                   "arrived yet (day 1 of the 3-day grace window; after it the defined max "
                                   "loss settles at zero frictions)")
    _run_tracker_with(monkeypatch, lambda: r)
    assert ("Plan tracker: live account lvW is waiting for bars — NIFTY 50 expired 2026-10-28 and no daily close "
            "on or before expiry has arrived yet (day 1 of the 3-day grace window") in capsys.readouterr().out
