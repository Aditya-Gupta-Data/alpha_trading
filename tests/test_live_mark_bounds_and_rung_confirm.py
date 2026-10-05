"""
Audit Chunk 2 F01 — a single impossible quote can never arm PAPER_2L_LIVE's
profit ratchet (src/execution/live_pricer.py).

1. A crossed mark is at or BELOW true value (longs at the bid, shorts at the
   ask), so a mark above the structure's upper bound — the width for a
   debit, 0 for a credit — by more than a tick is a bad print: the tick
   abstains with a named reason, persists nothing, fires no exit. Before:
   it was clamped to 100% capture (lock 70 latched; a condor's profit take
   and a pre-expiry exit booked full max profit).
2. A read that would raise the ratchet lock to a new rung is persisted only
   when a SECOND, later, independently fetched chain also clears that rung.
   Before: one in-bounds spike armed lock 70 for good, and the next
   ordinary mark below it was a forced exit #105 forbids.

Hermetic: the `world` fixture of tests/test_live_account.py (sqlite
':memory:', chains injected, the clock injected).
"""
from datetime import datetime

import pytest

from src import oms, portfolio_manager as pm
from src.execution import live_pricer as lp
from src.strategy import StrategyConstructor
from tests.test_live_account import LIVE, OPEN, _bull_call, _chain, _condor, _open, world  # noqa: F401

KEY_EXPIRY = "2026-10-27"


def _bear_put(expiry=KEY_EXPIRY):
    """The production position 24f931bb: bear put 13725/13625 PE, lot 120,
    crossed entry d 39.65 on a 100-wide spread (max profit 60.35)."""
    s = StrategyConstructor(vix=13.0, lot_size=120).construct_bear_put_spread(13725, 13625, 208.75, 169.10)
    for l in s["legs"]:
        l["fill_basis"] = "quoted"
    s.update(lots=1, expiry=expiry, entry_spot=13700.0)
    return s


def _pe(long_q, short_q):
    return _chain({(13725, "PE"): long_q, (13625, "PE"): short_q})


BEAR_ENTRY = _pe((205.0, 208.75, 207.0), (169.10, 172.0, 170.0))
# the sweep's production-shape bad print: each leg inside its 50% band, but
# long bid 300 - short ask 120 = 180 on a 100-wide spread (> the width)
IMPOSSIBLE = _pe((300.0, 305.0, 208.0), (115.0, 120.0, 169.0))
# an IN-BOUNDS spike: 250 - 152 = 98 -> profit 58.35 = 96.7% capture (would arm 90 -> 70)
SPIKE = _pe((250.0, 254.0, 208.0), (148.0, 152.0, 169.0))
SPIKE_2 = _pe((249.0, 253.0, 208.0), (148.0, 152.0, 169.0))      # 97 -> 95.0%: a second, distinct fetch
AT_85 = _pe((240.0, 244.0, 238.0), (146.0, 149.0, 150.0))        # 91 -> 85.1% (80 -> 50)
AT_62 = _pe((230.0, 234.0, 228.0), (150.0, 153.0, 151.0))        # 77 -> 61.9% (60 -> 30)
AT_45 = _pe((220.0, 224.0, 218.0), (150.0, 153.0, 151.0))        # 67 -> 45.3% (40 -> 0)
NORMAL = _pe((200.0, 204.0, 202.0), (163.0, 166.0, 164.5))       # 34 -> -9.4%


def _open_bear(c, ref, expiry=KEY_EXPIRY):
    _e, _v, opened = _open(c, ref, _bear_put(expiry), BEAR_ENTRY)
    assert (opened["entry_mark_ps"], opened["width_ps"], opened["max_profit_ps"], opened["max_loss_ps"]) == \
        (39.65, 100.0, 60.35, 39.65)
    return opened


def _tick(c, chain, epoch, now=OPEN):
    """One tick on an injected chain. A tick whose epoch is >= 300 s past the
    last fetch of that chain FETCHES (a new, independent snapshot); a closer
    one re-reads the cached snapshot."""
    return lp.tick(now=now, conn=c, chain_fn=lambda tk, x: chain, sleep_fn=lambda s: None,
                   now_epoch_fn=lambda: epoch, interval_s=300)


def _row(c, ref):
    return [p for p in lp.positions(c) if p["journal_ref"] == ref][0]


def _exit_tickets(c):
    return c.execute("SELECT COUNT(*) FROM trade_tickets WHERE note LIKE 'EXIT%'").fetchone()[0]


# ------------------------------------------------------------- 1. impossible marks

def test_an_impossible_mark_abstains_with_a_named_reason_within_a_one_tick_tolerance(world):
    c = world
    _open(c, "lv0700")                                   # bull call: d 70, width 200 -> upper bound 200
    row = lp.open_rows(c, LIVE)[0]
    today = OPEN.date()
    # a long bid on an untraded strike (no ask, no last) passes every per-leg check
    ev = lp.evaluate(row, _chain({(24000, "CE"): (280.0, None, None), (24200, "CE"): (33.0, 35.0, 34.0)}), today)
    assert not ev["ok"] and ev["signal"] is None
    assert ev["reason"] == "impossible mark 245 above the structure's upper bound 200"
    # one tick over the bound is tolerated (clamped to max profit); more is not
    ev = lp.evaluate(row, _chain({(24000, "CE"): (232.05, None, None), (24200, "CE"): (31.0, 32.0, 31.5)}), today)
    assert ev["ok"] and ev["mark_ps"] == 200.05 and ev["profit_ps"] == 130.0
    ev = lp.evaluate(row, _chain({(24000, "CE"): (232.1, None, None), (24200, "CE"): (31.0, 32.0, 31.5)}), today)
    assert not ev["ok"] and ev["reason"] == "impossible mark 200.1 above the structure's upper bound 200"
    # a mark AT the width is possible (deep ITM at expiry) and is a full capture
    ev = lp.evaluate(row, _chain({(24000, "CE"): (232.0, None, None), (24200, "CE"): (31.0, 32.0, 31.5)}), today)
    assert ev["ok"] and ev["capture_pct"] == 100.0
    # the loss side is unchanged: a far-below-value crossed mark still marks (clamped at max loss)
    ev = lp.evaluate(row, _chain({(24000, "CE"): (None, 1.0, None), (24200, "CE"): (0.5, 0.6, 0.55)}), today)
    assert ev["ok"] and ev["profit_ps"] == -70.0


def test_the_24f931bb_print_persists_nothing_and_fires_no_exit(world):
    c = world
    _open_bear(c, "lv0701")
    t = _tick(c, IMPOSSIBLE, 1000.0)
    assert t["fetched"] == 1 and t["abstained"] == 1 and t["marked"] == 0 and t["exits"] == []
    row = _row(c, "lv0701")
    assert row["state"] == "open"
    assert (row["last_mark_ps"], row["last_mark_ts"], row["quote_ts"], row["last_capture_pct"]) == \
        (None, None, None, None)
    assert (row["ratchet_peak_pct"], row["ratchet_lock_pct"]) == (None, None)
    assert lp._PENDING_RUNG == {}                        # not even a sighting
    # the next ordinary book: no lock was ever armed, so no 'profit ratchet' exit at a loss
    t = _tick(c, NORMAL, 1400.0)
    assert t["exits"] == [] and _row(c, "lv0701")["state"] == "open"
    assert _row(c, "lv0701")["ratchet_lock_pct"] is None and _exit_tickets(c) == 0


def test_a_pre_expiry_exit_never_settles_on_an_impossible_print(world):
    c = world
    _open_bear(c, "lv0702", expiry="2026-09-30")         # 1 day left on 09-29: the pre-expiry window
    t = _tick(c, IMPOSSIBLE, 1000.0)
    assert t["exits"] == [] and _row(c, "lv0702")["state"] == "open" and _exit_tickets(c) == 0
    # the next usable read exits at ITS price, not at a clamped full max profit
    t = _tick(c, NORMAL, 1400.0)
    assert [x["signal"] for x in t["exits"]] == ["pre_expiry_exit"] and t["exits"][0]["status"] == "settled"
    row = _row(c, "lv0702")
    assert row["exit_mark_ps"] == 34.0 and row["last_profit_ps"] == -5.65 and row["pnl_net"] < 0


def test_a_condor_that_would_pay_you_to_close_is_a_bad_print_not_max_profit(world):
    c = world
    entry = _chain({(23500, "PE"): (30.0, 32.0, 31.0), (23300, "PE"): (9.0, 10.0, 9.5),
                    (24500, "CE"): (30.0, 32.0, 31.0), (24700, "CE"): (9.0, 10.0, 9.5)})
    _open(c, "lv0703", _condor(), entry)                 # credit 40 -> upper bound 0
    # the 23300 wing BID (12, no ask/last) above the 23500 short's ASK (6): mark 12 + 1 - 6 - 6 = +1
    bad = _chain({(23500, "PE"): (5.0, 6.0, 5.5), (23300, "PE"): (12.0, 0.0, 0.0),
                  (24500, "CE"): (5.0, 6.0, 5.5), (24700, "CE"): (1.0, 1.5, 1.2)})
    ev = lp.evaluate(lp.open_rows(c, LIVE)[0], bad, OPEN.date())
    assert not ev["ok"] and ev["reason"] == "impossible mark 1 above the structure's upper bound 0"
    t = _tick(c, bad, 1000.0)
    assert t["abstained"] == 1 and t["exits"] == [] and _row(c, "lv0703")["state"] == "open"
    # a real decayed book still takes the 65% profit at its real price
    decayed = _chain({(23500, "PE"): (5.0, 6.0, 5.5), (23300, "PE"): (1.0, 2.0, 1.5),
                      (24500, "CE"): (5.0, 6.0, 5.5), (24700, "CE"): (1.0, 2.0, 1.5)})
    t = _tick(c, decayed, 1400.0)
    assert [(x["signal"], x["status"]) for x in t["exits"]] == [("profit_take", "settled")]
    row = _row(c, "lv0703")
    assert row["exit_mark_ps"] == -10.0 and row["last_profit_ps"] == 30.0          # 75%, not the full 40


def test_the_exit_door_itself_refuses_an_impossible_mark(world):
    c = world
    _open_bear(c, "lv0704")
    row = dict(lp.open_rows(c, LIVE)[0], ratchet_lock_pct=0.0)
    res = lp._exit(c, row, {(13725.0, "PE"): 300.0, (13625.0, "PE"): 120.0}, "pre_expiry_exit", OPEN)
    assert res == {"status": "held_impossible_mark", "journal_ref": "lv0704",
                   "reason": "impossible mark 180 above the structure's upper bound 100"}
    assert _exit_tickets(c) == 0 and _row(c, "lv0704")["state"] == "open"
    assert pm._active_shadow_lock(c, LIVE, "lv0704") is not None
    # the loss-side twin is unchanged
    res = lp._exit(c, row, {(13725.0, "PE"): 120.0, (13625.0, "PE"): 300.0}, "pre_expiry_exit", OPEN)
    assert res["status"] == "held_loss_beyond_max"


# ------------------------------------------------------------- 2. a new rung needs a second fetch

def test_an_in_bounds_one_snapshot_spike_does_not_arm_the_lock(world):
    c = world
    _open_bear(c, "lv0710")
    t = _tick(c, SPIKE, 1000.0)
    assert t["marked"] == 1 and t["exits"] == [] and t["rung_pending"] == 1
    row = _row(c, "lv0710")
    # the mark is recorded; the peak and lock keep their (empty) confirmed values
    assert row["last_mark_ps"] == 98.0 and row["last_capture_pct"] == pytest.approx(96.69, abs=0.01)
    assert (row["ratchet_peak_pct"], row["ratchet_lock_pct"]) == (None, None)
    # the next fetch is an ordinary book: before F01 this was ratchet_hit at a loss against lock 70
    t = _tick(c, NORMAL, 1400.0)
    assert t["exits"] == [] and _exit_tickets(c) == 0
    row = _row(c, "lv0710")
    assert row["state"] == "open" and row["ratchet_lock_pct"] is None
    assert row["ratchet_peak_pct"] == pytest.approx(-9.36, abs=0.01)
    assert lp._PENDING_RUNG == {}                        # the contrary read dropped the sighting


def test_two_consecutive_fresh_fetches_at_the_rung_arm_it_and_only_then_can_it_exit(world):
    c = world
    _open_bear(c, "lv0711")
    assert _tick(c, SPIKE, 1000.0)["rung_pending"] == 1
    t = _tick(c, SPIKE_2, 1400.0)                        # a second, later fetch: 95.0% also clears 90
    assert t["fetched"] == 1 and t["rung_confirmed"] == 1 and t["exits"] == []
    row = _row(c, "lv0711")
    assert row["ratchet_lock_pct"] == 70.0
    assert row["ratchet_peak_pct"] == 95.02          # what BOTH reads cleared (the lower), floored (#122)
    assert lp._PENDING_RUNG == {}
    # a confirmed lock is real: the give-back below it exits (on a fresh chain)
    t = _tick(c, NORMAL, 1800.0)
    assert [(x["signal"], x["status"]) for x in t["exits"]] == [("ratchet_hit", "settled")]


def test_the_same_cached_snapshot_reread_on_the_next_tick_confirms_nothing(world):
    c = world
    _open_bear(c, "lv0712")
    _tick(c, SPIKE, 1000.0)
    for epoch in (1060.0, 1120.0, 1180.0):              # 60-s ticks inside the 300-s quote interval
        t = _tick(c, SPIKE, epoch)
        assert t["fetched"] == 0 and t["rung_pending"] == 1
        assert _row(c, "lv0712")["ratchet_lock_pct"] is None
    t = _tick(c, SPIKE, 1400.0)                          # a real second fetch
    assert t["fetched"] == 1 and t["rung_confirmed"] == 1 and _row(c, "lv0712")["ratchet_lock_pct"] == 70.0


def test_a_restart_between_the_two_reads_needs_reconfirmation(world):
    c = world
    _open_bear(c, "lv0713")
    _tick(c, SPIKE, 1000.0)
    lp.reset_cache()                                     # the live loop restarts: process memory is gone
    t = _tick(c, SPIKE, 1400.0)
    assert t["rung_pending"] == 1 and _row(c, "lv0713")["ratchet_lock_pct"] is None
    t = _tick(c, SPIKE_2, 1800.0)
    assert t["rung_confirmed"] == 1 and _row(c, "lv0713")["ratchet_lock_pct"] == 70.0


def test_the_two_reads_confirm_only_the_rung_both_cleared(world):
    c = world
    _open_bear(c, "lv0714")
    _tick(c, SPIKE, 1000.0)                              # 96.7% (90 -> 70) ...
    t = _tick(c, AT_85, 1400.0)                          # ... then 85.1%: both cleared 80 -> 50, not 90
    assert t["rung_confirmed"] == 1
    row = _row(c, "lv0714")
    assert row["ratchet_lock_pct"] == 50.0 and row["ratchet_peak_pct"] == pytest.approx(85.08, abs=0.01)
    t = _tick(c, SPIKE, 1800.0)                          # 70 needs its own two reads
    assert t["rung_pending"] == 1 and _row(c, "lv0714")["ratchet_lock_pct"] == 50.0
    t = _tick(c, SPIKE_2, 2200.0)
    assert t["rung_confirmed"] == 1 and _row(c, "lv0714")["ratchet_lock_pct"] == 70.0


def test_a_contrary_read_resets_the_sighting_but_an_abstention_does_not(world):
    c = world
    _open_bear(c, "lv0715")
    _tick(c, SPIKE, 1000.0)
    _tick(c, NORMAL, 1400.0)                             # contrary: the sighting is forgotten
    t = _tick(c, SPIKE_2, 1800.0)
    assert t["rung_pending"] == 1 and _row(c, "lv0715")["ratchet_lock_pct"] is None
    t = _tick(c, IMPOSSIBLE, 2200.0)                     # a bad print is no evidence either way
    assert t["abstained"] == 1 and _row(c, "lv0715")["ratchet_lock_pct"] is None
    t = _tick(c, SPIKE, 2600.0)
    assert t["rung_confirmed"] == 1 and _row(c, "lv0715")["ratchet_lock_pct"] == 70.0


def test_a_ratchet_hit_fires_only_against_the_confirmed_lock(world):
    c = world
    _open_bear(c, "lv0716")
    _tick(c, AT_45, 1000.0)
    _tick(c, AT_45, 1400.0)
    assert _row(c, "lv0716")["ratchet_lock_pct"] == 0.0             # breakeven lock, confirmed
    t = _tick(c, SPIKE, 1800.0)                                     # one spike: 70 is only SEEN
    assert t["rung_pending"] == 1 and _row(c, "lv0716")["ratchet_lock_pct"] == 0.0
    # 61.9% is below the unconfirmed 70 (before F01: ratchet_hit) but above the confirmed 0:
    # no exit; the two reads agree on 61.9% -> the 60 -> 30 rung arms
    t = _tick(c, AT_62, 2200.0)
    assert t["exits"] == [] and t["rung_confirmed"] == 1
    assert _row(c, "lv0716")["state"] == "open" and _row(c, "lv0716")["ratchet_lock_pct"] == 30.0


def test_a_reverify_fetch_is_the_source_of_its_own_read(world):
    c = world
    _open_bear(c, "lv0717")
    _tick(c, AT_45, 1000.0)
    _tick(c, AT_45, 1400.0)
    assert _row(c, "lv0717")["ratchet_lock_pct"] == 0.0
    # a cached give-back fires ratchet_hit; its re-verify fetch shows the spike instead
    lp._CHAIN_CACHE[("NIFTY 50", KEY_EXPIRY)] = (1500.0, "2026-09-29T11:00:00", NORMAL)
    t = _tick(c, SPIKE, 1560.0)
    assert t["fetched"] == 1 and t["exits"] == [] and t["rung_pending"] == 1
    # the next 60-s tick re-reads THAT re-verify snapshot from the cache: still one snapshot
    t = _tick(c, SPIKE, 1620.0)
    assert t["fetched"] == 0 and t["rung_pending"] == 1 and _row(c, "lv0717")["ratchet_lock_pct"] == 0.0


def test_a_closed_positions_sighting_is_dropped(world):
    c = world
    _open_bear(c, "lv0718")
    _tick(c, SPIKE, 1000.0)
    assert list(lp._PENDING_RUNG) == [(LIVE, "lv0718")]
    c.execute("UPDATE paper_live_positions SET state = 'closed' WHERE journal_ref = 'lv0718'")
    c.commit()
    _tick(c, SPIKE, 1400.0)
    assert lp._PENDING_RUNG == {}


def test_a_read_that_confirms_a_lower_rung_is_the_first_sighting_of_its_own_higher_one(world):
    c = world
    _open_bear(c, "lv0719")
    _tick(c, AT_85, 1000.0)                              # 85.1% (80 -> 50) ...
    t = _tick(c, SPIKE, 1400.0)                          # ... then 96.7%: both cleared 80 -> 50 is confirmed
    assert t["rung_confirmed"] == 1 and _row(c, "lv0719")["ratchet_lock_pct"] == 50.0
    t = _tick(c, SPIKE_2, 1800.0)                        # and the 96.7% read was the first sighting of 70
    assert t["rung_confirmed"] == 1 and _row(c, "lv0719")["ratchet_lock_pct"] == 70.0


def test_a_sighting_is_judged_against_the_record_as_it_stands(world):
    c = world
    _open_bear(c, "lv0720")
    _tick(c, AT_45, 1000.0)                              # sighting: 45.3% (40 -> 0)
    c.execute("UPDATE paper_live_positions SET ratchet_peak_pct = 61.0, ratchet_lock_pct = 30.0 "
              "WHERE journal_ref = 'lv0720'")             # the record moved under it (lock 30)
    c.commit()
    t = _tick(c, AT_85, 1400.0)                          # agreed 45.3% adds nothing above 30: start over
    assert t["rung_pending"] == 1 and "rung_confirmed" not in t
    assert _row(c, "lv0720")["ratchet_lock_pct"] == 30.0
    t = _tick(c, AT_85, 1800.0)
    assert t["rung_confirmed"] == 1 and _row(c, "lv0720")["ratchet_lock_pct"] == 50.0
