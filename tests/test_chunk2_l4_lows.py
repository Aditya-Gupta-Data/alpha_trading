"""
PAPER_2L_LIVE + the intraday square-off — audit Chunk 2 lows F10, F19, F20
and F24 (2026-10-06, batch L4). Each test turns an audit repro around: it
asserts the FIXED behaviour.

F10: the dashboard's open-trades table showed PAPER_2L_LIVE on the
primary's model MTM and ratchet, and dropped the position when the primary
settled while the live arm still held it. Now the arm's position is a row
of its own from paper_live_positions (its own mark, capture, peak/lock,
quote time and the Fix D stale flag) and outlives the primary's settlement.

F19: a second LIVE position on a cached (ticker, expiry) was marked first on
a chain fetched BEFORE it existed; with the door failing, an old chain was
re-stamped as a fresh mark every tick. Now a pre-entry chain never marks the
row (its key is due at once; until fetched the row abstains, named), a mark's
time is its chain's fetch time, and a chain older than the quote interval
does not move the ratchet.

F20: the #110 kill switch (ratchet_enabled) did not reach the live arm. Now
all three exit paths ask ONE predicate, profit_ratchet.applies.

F24: the intraday square-off got one look per (trade, signal) per session.
Now a declined square-off is retried while the signal persists — once per
quote interval per trade, the outcome printed once per change.

L4 review (2026-10-07): the retry branch's `squarable` gate is pinned; the
quote door is quiet inside the square-off (its reason rides the bridge's
deduped line); a row opened after the tick began abstains on THIS tick's
chain under its own name, with no window event; the stale-chain peak
freeze is pinned inside a rung and at the interval boundary.

Hermetic: ':memory:' brain maps or the per-test tmp file, tmp journal
(conftest), injected chains / clocks / quote doors, Discord captured.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from src import brain_map, journal, live_bridge as lb, oms, plan_tracker as pt
from src import portfolio_manager as pm, profit_ratchet as pr, strategy_router as sr
from src.dashboard import data as dash
from src.execution import live_pricer as lp, paper_venue as pv
from src.strategy import StrategyConstructor

LIVE, TWO_L, ROT = pm.ACCOUNT_PAPER_2L_LIVE, pm.ACCOUNT_PAPER_2L, pm.ACCOUNT_PAPER_2L_ROT
IST = timezone(timedelta(hours=5, minutes=30))


def _chain(quotes, spot=24000.0):
    oc = {}
    for (k, t), (bid, ask, ltp) in quotes.items():
        oc.setdefault(f"{float(k):.6f}", {})[t.lower()] = {"top_bid_price": bid, "top_ask_price": ask,
                                                           "last_price": ltp}
    return {"last_price": spot, "oc": oc}


class _KeepOpen:
    """A shared connection every callee's close() leaves open."""

    def __init__(self, c):
        object.__setattr__(self, "_c", c)

    def __getattr__(self, n):
        return getattr(self._c, n)

    def __setattr__(self, n, v):
        setattr(self._c, n, v)

    def close(self):
        pass


@pytest.fixture
def world(monkeypatch):
    raw = brain_map.connect(":memory:")
    c = _KeepOpen(raw)
    pm.ensure_accounts_schema(c)
    oms.ensure_schema(c)
    lp.ensure_schema(c)
    pm.get_account(c)
    monkeypatch.setattr(brain_map, "connect", lambda *a, **k: c)
    monkeypatch.setattr(pt, "_brain_connect", lambda *a, **k: c)
    monkeypatch.setattr(pt, "journal", journal)              # another file's FakeJournal never leaks in
    monkeypatch.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", True)
    monkeypatch.setattr("src.config.PAPER_VENUE_ENABLED", True)
    monkeypatch.setattr("src.config.RATCHET_ENABLED", True)
    monkeypatch.setattr(pv, "_tier_frac", lambda u, slippage_fn=None: 0.01)
    monkeypatch.setattr(lp, "_market_open", lambda now: True)
    monkeypatch.setattr(lp, "_stamp_journal", lambda row, payload: None)
    lp.reset_cache()
    yield c
    lp.reset_cache()
    raw.close()


def _open_live(c, ref, spread, chain, at, ticker="NIFTY 50"):
    """The real entry path for PAPER_2L_LIVE at `at`: requote -> ticket ->
    venue -> open_position (entry_quote_ts / opened_at = `at`)."""
    e = {"short_id": ref, "date": at.date().isoformat(), "ticker": ticker, "spread": spread, "signal": "t",
         "accounts": {LIVE: {"status": "approved", "lots": 1}}}
    pm.paper_request_entry(c, LIVE, ref, 15000.0, lots=1, primary_lots=1)
    rq = lp.requote_entry(e, now=at, chain_fn=lambda t, x: chain)
    assert rq["ok"], rq
    iss = sr.issue(c, {"ticker": ticker, "short_id": ref, "signal": "t", "spread": dict(spread, legs=rq["legs"])},
                   journal_ref=ref, source="t", account_id=LIVE, lots=1)
    pv.sweep(c, stamp=False)
    view = oms.ticket_view(c, iss["ticket_id"])
    assert view["status"] == oms.FILLED
    return lp.open_position(c, LIVE, e, view, quote_ts=rq["quote_ts"], now=at)


def _row(c, ref):
    return [r for r in lp.positions(c) if r["journal_ref"] == ref][0]


def _quoted(s, expiry):
    for leg in s["legs"]:
        leg["fill_basis"] = "quoted"
    s.update(lots=1, expiry=expiry, entry_spot=24000.0)
    return s


# =================================================================== F10 — the dashboard

def _bear_put_mid(lots=3, basis="venue"):
    return {"strategy": "bear_put_spread", "direction": "bearish", "expiry": "2026-10-27",
            "lot_size": 120, "lots": lots, "entry_spot": 13731.65, "spread_width": 100.0,
            "max_profit": 7236.0, "max_loss": 4764.0, "reward_risk": 1.52,
            "margin": {"total_margin": 16764.0},
            "legs": [{"side": "BUY", "option_type": "PE", "strike": 13725.0, "premium": 209.11, "fill_basis": basis},
                     {"side": "SELL", "option_type": "PE", "strike": 13625.0, "premium": 169.03,
                      "fill_basis": basis}]}


F10_REF, F10_AT = "24f931bb", datetime(2026, 9, 30, 10, 58)
MID_ENTRY = _chain({(13725, "PE"): (207.0, 208.75, 208.0), (13625, "PE"): (169.10, 170.5, 170.0)}, 13700.0)
MID_BARS = [("2026-09-30", 13700.0, 13760.0, 13731.65),
            ("2026-10-01", 13630.0, 13740.0, 13640.0),
            ("2026-10-05", 13660.0, 13700.0, 13680.0)]


def _f10_book(c, monkeypatch):
    """24f931bb as on the VM: the primary + ROT + LIVE hold it; the
    primary's model ratchet is armed (peak 100 / lock 70) while LIVE's own
    is peak 29.32, unarmed, last mark 0.30/share of profit."""
    monkeypatch.setattr(lp, "_now", lambda: F10_AT)
    monkeypatch.setattr(pt, "_settle_spread_cash", lambda pnl: True)
    pm.request_entry(c, F10_REF, 3 * 16764.0)
    pm.paper_request_entry(c, ROT, F10_REF, 16764.0, lots=1, primary_lots=3)
    pm.paper_request_entry(c, LIVE, F10_REF, 16764.0, lots=1, primary_lots=3)
    e = {"short_id": F10_REF, "ticker": "NIFTY MID SELECT", "spread": _bear_put_mid(basis="quoted"), "signal": "t"}
    rq = lp.requote_entry(e, now=F10_AT, chain_fn=lambda t, x: MID_ENTRY)
    issued = sr.issue(c, {"ticker": e["ticker"], "short_id": F10_REF, "signal": "t",
                          "spread": dict(e["spread"], legs=rq["legs"])},
                      journal_ref=F10_REF, source="t", account_id=LIVE, lots=1)
    pv.sweep(c, stamp=False)
    lp.open_position(c, LIVE, e, oms.ticket_view(c, issued["ticket_id"]), quote_ts=rq["quote_ts"], now=F10_AT)
    c.execute("UPDATE paper_live_positions SET ratchet_peak_pct = 29.32, ratchet_lock_pct = NULL, "
              "last_profit_ps = 0.30, last_capture_pct = 0.5, last_mark_ps = 40.38, "
              "last_mark_ts = '2026-10-01T11:00:00', quote_ts = '2026-10-01T11:00:00' WHERE journal_ref = ?",
              (F10_REF,))
    c.commit()
    journal.log({"short_id": F10_REF, "date": "2026-09-30", "created_at": "2026-09-30T10:58:05+05:30",
                 "ticker": "NIFTY MID SELECT", "action": "SPREAD", "decision": "approved", "why": "t",
                 "signal": "t", "price": 40.08, "spread": _bear_put_mid(),
                 "ratchet": {"peak_capture_pct": 100.0, "locked_pct": 70.0, "armed": True, "as_of": "2026-10-01",
                             "rungs": [{"as_of": "2026-10-01", "peak_capture_pct": 96.87, "locked_pct": 70.0}]},
                 "accounts": {ROT: {"status": "approved", "lots": 1, "margin_rs": 16764.0, "ticket_id": "tkt:rot"},
                              LIVE: {"status": "approved", "lots": 1, "margin_rs": 16764.0,
                                     "ticket_id": "tkt:live"}},
                 "outcome": None})


def test_f10_the_live_arm_is_its_own_row_and_outlives_the_primarys_settlement(world, monkeypatch, tmp_path):
    """repro test_f10_verify_dashboard_live_rows, through the REAL primary
    settlement (plan_tracker._settle_spread_row under the journal lock)."""
    c = world
    _f10_book(c, monkeypatch)
    eq = tmp_path / "eq.jsonl"
    eq.write_text("")
    snap = {F10_REF: {"short_id": F10_REF, "live_pnl_rs": 21000.0, "capture_pct": 63.0}}   # the PRIMARY's mark
    now = datetime(2026, 10, 1, 11, 4)                    # 4 market-minutes after LIVE's mark: fresh
    rows = dash.open_trades(journal_path=journal.JOURNAL_PATH, equity_ledger_path=eq, snapshot_marks=snap,
                            live=dash._live_positions(c), now=now)
    by = {(r["account"], r["id"]): r for r in rows}
    # distinct ids (the React desk keys rows by id); the live row names its ref
    assert set(by) == {("PAPER_10L", F10_REF), (LIVE, f"{LIVE}:{F10_REF}")}
    primary = by[("PAPER_10L", F10_REF)]
    # the primary's row: its own model figures; LIVE is no longer listed on it (it has its own row)
    assert primary["accounts"] == f"PAPER_10L, {ROT}" and primary["lots"] == 3
    assert (primary["mtm_rs"], primary["capture_pct"]) == (21000.0, 63.0)
    assert (primary["ratchet_peak_pct"], primary["ratchet_lock_pct"], primary["ratchet"]) == \
        (100.0, 70.0, "armed → lock 70.0%")
    live = by[(LIVE, f"{LIVE}:{F10_REF}")]
    assert live["journal_ref"] == F10_REF
    # the live arm's row: ITS figures from paper_live_positions — never the primary's
    assert live["accounts"] == LIVE and live["lots"] == 1 and live["entered"] == "2026-09-30"
    assert live["mtm_rs"] == round(0.30 * 1 * 120, 2) and live["capture_pct"] == 0.5
    assert (live["ratchet_peak_pct"], live["ratchet_lock_pct"], live["ratchet"]) == (29.32, None, "unarmed")
    assert live["last_mark_ts"] == "2026-10-01T11:00:00" and live["mark_stale"] is False
    assert live["primary_settled"] is False and live["note"].startswith("live arm's own position")
    assert live["max_loss_rs"] == round(lp.open_rows(c, LIVE)[0]["max_loss_ps"] * 120, 2)

    # the REAL primary settlement (model ratchet_hit); LIVE keeps its position and lock
    seen = {}
    journal.update_matching(lambda e: journal.row_key(e) == F10_REF,
                            lambda e: pt._settle_spread_row(e, MID_BARS, seen))
    assert seen["status"] == "resolved" and seen["resolution"] == "ratchet_hit"
    assert lp.has_open_position(c, LIVE, F10_REF) and pm._active_shadow_lock(c, LIVE, F10_REF) == (16764.0, 1)

    after = dash.open_trades(journal_path=journal.JOURNAL_PATH, equity_ledger_path=eq, snapshot_marks=snap,
                             live=dash._live_positions(c), now=datetime(2026, 10, 1, 12, 0))
    assert [(r["account"], r["journal_ref"]) for r in after] == [(LIVE, F10_REF)]   # still an open row
    r = after[0]
    assert r["primary_settled"] is True and "the primary settled (ratchet_hit" in r["note"]
    assert "this position is still open" in r["note"]
    assert r["mark_stale"] is True                          # an hour of market time since its mark: flagged
    # the treasury card: one reader, the same answer — the primary flat, LIVE holding one
    u = dash.unrealized_by_account(c, snapshot={"marks": []}, rows=None, now=datetime(2026, 10, 1, 12, 0))
    assert u["PAPER_10L"]["open_positions"] == 0
    assert u[LIVE]["open_positions"] == 1 and u[LIVE]["unrealized_pnl"] == 36.0


def test_f10_open_trades_reads_the_live_book_itself_read_only(tmp_path):
    """app.py / api_bridge call open_trades() bare: it reads the live book
    from the database through a read-only connection."""
    db = tmp_path / "bm.db"
    c = brain_map.connect(str(db))
    lp.ensure_schema(c)
    c.execute("INSERT INTO paper_live_positions (account_id, journal_ref, ticker, strategy, direction, expiry, lots, "
              "lot_size, legs_json, entry_mark_ps, width_ps, max_profit_ps, max_loss_ps, opened_at, "
              "ratchet_peak_pct, ratchet_lock_pct, last_mark_ts, quote_ts, last_profit_ps, last_capture_pct, state) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (LIVE, "lv10a", "NIFTY 50", "bull_call_spread", "bullish", "2026-10-28", 2, 65, "[]", 70.0, 200.0,
               130.0, 70.0, "2026-10-27T10:00:00", 61.0, 30.0, "2026-10-27T10:58:00", "2026-10-27T10:58:00",
               80.0, 61.54, "open"))
    c.commit()
    c.close()
    j = tmp_path / "j.jsonl"
    j.write_text("")
    eq = tmp_path / "eq.jsonl"
    eq.write_text("")
    import os
    os.utime(db, (datetime(2026, 10, 27, 10, 59, 30, tzinfo=IST).timestamp(),) * 2)    # the copy's capture time
    rows = dash.open_trades(j, eq, snapshot_marks={}, db_path=db, now=datetime(2026, 10, 27, 11, 30))
    assert len(rows) == 1
    r = rows[0]
    assert (r["account"], r["id"], r["accounts"], r["lots"]) == (LIVE, f"{LIVE}:lv10a", LIVE, 2)
    assert r["mtm_rs"] == 80.0 * 2 * 65 and r["ratchet"] == "armed → lock 30.0%" and r["primary_settled"] is False
    assert r["mark_stale"] is False                 # judged as of the copy (Fix D review), not the viewer's 11:30
    os.utime(db, (datetime(2026, 10, 27, 11, 20, tzinfo=IST).timestamp(),) * 2)
    assert dash.open_trades(j, eq, snapshot_marks={}, db_path=db,
                            now=datetime(2026, 10, 27, 11, 30))[0]["mark_stale"] is True


def test_f10_an_unreadable_live_book_keeps_the_arm_named_on_the_primary_row(tmp_path):
    """Abstention, never a silent drop: with the book unreadable the arm
    stays listed on the primary's row (as before F10), and no live row is
    invented."""
    j = tmp_path / "j.jsonl"
    import json
    j.write_text(json.dumps({"short_id": "aa11", "ticker": "NIFTY 50", "date": "2026-10-01", "decision": "approved",
                             "outcome": None, "spread": {"strategy": "bear_put_spread", "lots": 1, "max_loss": 1.0},
                             "accounts": {LIVE: {"status": "approved"}, TWO_L: {"status": "approved"}}}) + "\n")
    eq = tmp_path / "eq.jsonl"
    eq.write_text("")
    rows = dash.open_trades(j, eq, snapshot_marks={}, db_path=tmp_path / "missing.db")
    assert [r["accounts"] for r in rows] == [f"PAPER_10L, {LIVE}, {TWO_L}"]
    rows = dash.open_trades(j, eq, snapshot_marks={}, live={"error": "database is locked"})
    assert [r["account"] for r in rows] == ["PAPER_10L"] and LIVE in rows[0]["accounts"]


def test_l3c_an_already_open_verdict_holds_the_trade_on_the_table(tmp_path):
    """L3 residual (c): a re-approved LIVE position's verdict reads
    'already_open' — it was missing from the accounts list until it closed."""
    import json
    j = tmp_path / "j.jsonl"
    j.write_text(json.dumps({"short_id": "ao01", "ticker": "NIFTY 50", "date": "2026-10-01", "decision": "approved",
                             "outcome": None, "spread": {"strategy": "bull_call_spread", "lots": 1, "max_loss": 1.0},
                             "accounts": {LIVE: {"status": "already_open", "lots": 1},
                                          TWO_L: {"status": "rejected"}}}) + "\n")
    eq = tmp_path / "eq.jsonl"
    eq.write_text("")
    rows = dash.open_trades(j, eq, snapshot_marks={}, live={"error": "unreadable"})
    assert rows[0]["accounts"] == f"PAPER_10L, {LIVE}"
    assert dash.HOLDING_VERDICTS == ("approved", "already_open")


# =================================================================== F19 — chain age

EXP = "2026-10-28"
E0 = datetime(2026, 10, 6, 10, 0, tzinfo=IST).timestamp()            # 10:00 IST on the epoch clock
# 10:00 — spot ~23900
C0 = _chain({(24000, "CE"): (60.0, 62.0, 61.0), (24200, "CE"): (15.0, 17.0, 16.0),
             (24000, "PE"): (210.0, 212.0, 211.0), (23800, "PE"): (83.0, 85.0, 84.0)})
# 10:02 onwards — a rally: puts cheaper, calls dearer
C1 = _chain({(24000, "CE"): (120.0, 122.0, 121.0), (24200, "CE"): (35.0, 37.0, 36.0),
             (24000, "PE"): (130.0, 132.0, 131.0), (23800, "PE"): (60.0, 62.0, 61.0)})


def _bull_call():
    return _quoted(StrategyConstructor(vix=13.0, lot_size=65).construct_bull_call_spread(24000, 24200, 62.0, 15.0),
                   EXP)


def _bear_put():
    return _quoted(StrategyConstructor(vix=13.0, lot_size=65).construct_bear_put_spread(24000, 23800, 132.0, 60.0),
                   EXP)


def _tick(c, at, epoch, chain_fn, **kw):
    kw.setdefault("interval_s", 300)
    return lp.tick(now=at, conn=c, chain_fn=chain_fn, sleep_fn=lambda s: None, now_epoch_fn=lambda: epoch, **kw)


def _at(h, m):
    return datetime(2026, 10, 6, h, m)


def test_f19_a_new_position_on_a_cached_key_is_marked_on_a_fresh_chain_never_the_pre_entry_one(world):
    """repro test_sf4_preentry_cached_chain_peak: B was marked on the 10:00
    chain at 10:03 (capture 41.4%, a rung sighting), and the 10:05 chain
    fired ratchet_hit at a loss 3 minutes after entry."""
    c = world
    _open_live(c, "A", _bull_call(), C0, _at(9, 55))
    assert _tick(c, _at(10, 0), E0, lambda t, x: C0)["fetched"] == 1
    opened = _open_live(c, "B", _bear_put(), C1, _at(10, 2))
    assert opened["entry_mark_ps"] == 72.0 and opened["max_profit_ps"] == 128.0
    # 10:03: the cache is 180 s old (inside the interval) but predates B -> its key is due NOW
    out = _tick(c, _at(10, 3), E0 + 180, lambda t, x: C1)
    assert out["fetched"] == 1 and out["exits"] == [] and out["abstained"] == 0 and out["marked"] == 2
    b = _row(c, "B")
    # crossed close on C1: 130 - 62 = 68 -> profit -4 -> capture -3.13%: no peak to speak of, no sighting
    assert b["last_mark_ps"] == 68.0 and b["ratchet_peak_pct"] == -3.13 and b["ratchet_lock_pct"] is None
    assert b["quote_ts"] == b["last_mark_ts"] == "2026-10-06T10:03:00" and b["quote_ts"] >= b["opened_at"]
    assert (LIVE, "B") not in lp._PENDING_RUNG
    out = _tick(c, _at(10, 5), E0 + 301, lambda t, x: C1)
    assert out["exits"] == [] and _row(c, "B")["state"] == "open"


def test_f19_a_pre_entry_chain_is_never_a_rung_sighting_while_the_row_waits(world):
    """No fetch possible this tick (cap 0): B abstains, NAMED, and the 10:00
    chain — 41.4% on B — never becomes one of the rung's two reads."""
    c = world
    _open_live(c, "A", _bull_call(), C0, _at(9, 55))
    _tick(c, _at(10, 0), E0, lambda t, x: C0)
    _open_live(c, "B", _bear_put(), C1, _at(10, 2))
    out = _tick(c, _at(10, 3), E0 + 180, lambda t, x: C1, max_fetches=0)
    assert out["fetched"] == 0 and out["marked"] == 1 and out["abstained"] == 1
    (note,) = [n for n in out["row_notes"] if n["journal_ref"] == "B"]
    assert note["kind"] == "abstained" and note["reason"] == (
        "no chain since this position opened: the cached chain (2026-10-06T10:00:00) predates its entry "
        "(2026-10-06T10:02:00) and the per-tick fetch cap or time budget was reached — it waits for a fresh fetch")
    b = _row(c, "B")
    assert (b["last_mark_ps"], b["last_mark_ts"], b["ratchet_peak_pct"]) == (None, None, None)
    assert (LIVE, "B") not in lp._PENDING_RUNG
    # past the cutoff the same row waits too, named so
    out = _tick(c, _at(15, 28), E0 + 240, lambda t, x: C1)
    assert "predates its entry (2026-10-06T10:02:00) and past the fetch cutoff" in \
        [n for n in out["row_notes"] if n["journal_ref"] == "B"][0]["reason"]


def test_f19_a_failing_door_never_restamps_an_old_chain_as_a_fresh_mark(world, capsys):
    """repro test_failing_door_remarks_an_old_chain_with_a_fresh_last_mark_ts:
    both rows were marked on the 31-minute-old chain with last_mark_ts 10:31
    and B armed off a chain 30 minutes older than B."""
    c = world
    _open_live(c, "A", _bull_call(), C0, _at(9, 55))
    _tick(c, _at(10, 0), E0, lambda t, x: C0)

    def down(t, x):
        raise lp.ChainUnavailable("rate limit (DH-904: Too many requests)")
    _open_live(c, "B", _bear_put(), C1, _at(10, 30))
    out = _tick(c, _at(10, 31), E0 + 1860, down)
    assert out["marked"] == 1 and out["abstained"] == 1 and out["stale_marks"] == 1
    a, b = _row(c, "A"), _row(c, "B")
    assert a["last_mark_ts"] == a["quote_ts"] == "2026-10-06T10:00:00"     # the chain's time, not 10:31
    assert (b["last_mark_ts"], b["ratchet_lock_pct"], b["ratchet_peak_pct"]) == (None, None, None)
    assert "predates its entry (2026-10-06T10:30:00) and the fetch failed this tick" in \
        [n for n in out["row_notes"] if n["journal_ref"] == "B"][0]["reason"]
    assert "chain NIFTY 50 2026-10-28 failed" in capsys.readouterr().out
    # the dashboard (Fix D) now sees that mark's true age
    u = dash.unrealized_by_account(c, snapshot={"marks": []}, rows=[], now=datetime(2026, 10, 6, 10, 31))
    assert u[LIVE]["last_mark_ts"] == "2026-10-06T10:00:00" and u[LIVE]["mark_stale"] is True


def test_f19_a_chain_older_than_the_quote_interval_does_not_move_the_ratchet(world):
    """The door is down and the cache is 31 minutes old: the row is marked
    on it (as of its own time) but its peak does not move, and a pending
    rung sighting from an earlier fetch is neither confirmed nor forgotten
    on it — an old snapshot is no evidence either way."""
    c = world
    _open_live(c, "A", _bull_call(), C0, _at(9, 55))
    _tick(c, _at(10, 0), E0, lambda t, x: C0)
    up = _chain({(24000, "CE"): (140.0, 142.0, 141.0), (24200, "CE"): (60.0, 62.0, 61.0)})   # 78 -> 31/153 = 20%
    lp._CHAIN_CACHE[("NIFTY 50", EXP)] = (E0 + 10, "2026-10-06T10:00:10", up)
    c.execute("UPDATE paper_live_positions SET ratchet_peak_pct = 5.0 WHERE journal_ref = 'A'")
    c.commit()
    earlier = (E0 - 400, "2026-10-06T09:53:20", up)
    lp._PENDING_RUNG[(LIVE, "A")] = {"src": earlier, "capture": 45.0}
    hot = _chain({(24000, "CE"): (180.0, 182.0, 181.0), (24200, "CE"): (50.0, 52.0, 51.0)})   # 128 -> 81/153 = 53%
    lp._CHAIN_CACHE[("NIFTY 50", EXP)] = (E0 + 10, "2026-10-06T10:00:10", hot)
    out = _tick(c, _at(10, 31), E0 + 1860, lambda t, x: None)               # the door answers nothing
    assert out["marked"] == 1 and out["stale_marks"] == 1 and "rung_confirmed" not in out
    a = _row(c, "A")
    assert a["last_mark_ps"] == 128.0 and a["last_capture_pct"] == pytest.approx(52.94, abs=0.01)
    assert (a["ratchet_peak_pct"], a["ratchet_lock_pct"]) == (5.0, None)    # the ratchet did not move
    assert lp._PENDING_RUNG[(LIVE, "A")] == {"src": earlier, "capture": 45.0}
    # the next CURRENT chain is evidence again: it confirms against the earlier sighting (C's rule)
    out = _tick(c, _at(10, 36), E0 + 2160, lambda t, x: hot)
    assert out["fetched"] == 1 and out["rung_confirmed"] == 1
    a = _row(c, "A")
    assert a["ratchet_lock_pct"] == 0.0 and a["ratchet_peak_pct"] == 45.0


def test_f19_a_stale_chain_replaced_by_a_reverify_fetch_is_evidence_again(world):
    """The due fetch fails, a predicate fires on the stale cache, and the
    re-verify fetch SUCCEEDS: that read is on a current chain, so the
    ratchet moves on it as on any fresh read (the peak rises in its rung)."""
    c = world
    _open_live(c, "A", _bull_call(), C0, _at(9, 55))
    c.execute("UPDATE paper_live_positions SET ratchet_peak_pct = 45.0, ratchet_lock_pct = 0.0 "
              "WHERE journal_ref = 'A'")
    c.commit()
    dip = _chain({(24000, "CE"): (50.0, 52.0, 51.0), (24200, "CE"): (15.0, 17.0, 16.0)})    # 33: -9% < lock 0
    lp._CHAIN_CACHE[("NIFTY 50", EXP)] = (E0, "2026-10-06T10:00:00", dip)
    rich = _chain({(24000, "CE"): (155.0, 157.0, 156.0), (24200, "CE"): (30.0, 32.0, 31.0)})  # 123: 49.67%
    calls = []

    def flaky(t, x):
        calls.append(1)
        if len(calls) == 1:
            raise lp.ChainUnavailable("transport error")
        return rich
    out = _tick(c, _at(10, 6), E0 + 360, flaky)
    assert len(calls) == 2 and out["exits"] == [] and out.get("stale_marks") is None
    a = _row(c, "A")
    assert a["last_mark_ps"] == 123.0 and a["quote_ts"] == "2026-10-06T10:06:00"
    assert (a["ratchet_peak_pct"], a["ratchet_lock_pct"]) == (49.67, 0.0)


def test_f19_rung_confirmation_still_needs_two_fresh_fetches(world):
    """C's _confirm_rung keeps working on current chains: one fetch is a
    sighting, a second, later fetch arms the rung."""
    c = world
    _open_live(c, "A", _bull_call(), C0, _at(9, 55))
    hot = _chain({(24000, "CE"): (180.0, 182.0, 181.0), (24200, "CE"): (50.0, 52.0, 51.0)})
    out = _tick(c, _at(10, 0), E0, lambda t, x: hot)
    assert out["rung_pending"] == 1 and _row(c, "A")["ratchet_lock_pct"] is None
    out = _tick(c, _at(10, 1), E0 + 60, lambda t, x: hot)                  # the SAME snapshot: still pending
    assert out["fetched"] == 0 and out["rung_pending"] == 1 and _row(c, "A")["ratchet_lock_pct"] is None
    assert _row(c, "A")["last_mark_ts"] == "2026-10-06T10:00:00"            # marked on the 10:00 chain
    out = _tick(c, _at(10, 5), E0 + 300, lambda t, x: hot)
    assert out["fetched"] == 1 and out["rung_confirmed"] == 1 and _row(c, "A")["ratchet_lock_pct"] == 0.0


def test_f19_stamps_parse_and_order_on_the_tick_clock():
    row = {"opened_at": "2026-10-06T10:02:00", "entry_quote_ts": "2026-10-06T10:01:58"}
    assert lp._entered_at(row) == datetime(2026, 10, 6, 10, 2)
    assert lp._predates_entry(row, "2026-10-06T10:01:59") is True
    assert lp._predates_entry(row, "2026-10-06T10:02:00") is False          # the same second: not before
    assert lp._predates_entry({"opened_at": "garbage"}, "2026-10-06T10:00:00") is False   # cannot order: marks
    assert lp._predates_entry(row, None) is False


def _events(c, kind, ref=None):
    sql, args = "SELECT detail FROM paper_account_events WHERE account_id = ? AND event_type = ?", [LIVE, kind]
    if ref is not None:
        sql += " AND journal_ref = ?"
        args.append(ref)
    return [r[0] for r in c.execute(sql, args).fetchall()]


def test_f19_review_a_row_opened_after_the_tick_began_abstains_on_this_ticks_chain_named_so(world):
    """L4 review: an approval lands WHILE a tick runs. The tick began at
    10:03:00 and fetched the key fresh — stamped at its start — and the row
    opened at 10:03:20. It is not marked on that chain, but nothing is
    missing: the reason says so (it used to blame the fetch cap), no
    forced-exit-window event is written, and the next tick's fetch marks
    it. The same row on a cached pre-entry chain it could NOT refresh is
    still an event (the window is real)."""
    c = world
    near = "2026-10-08"                                     # 2 days out: inside the forced-exit window
    put = _quoted(StrategyConstructor(vix=13.0, lot_size=65).construct_bear_put_spread(24000, 23800, 132.0, 60.0),
                  near)
    _open_live(c, "B", put, C1, _at(10, 3) + timedelta(seconds=20))
    out = _tick(c, _at(10, 3), E0 + 180, lambda t, x: C1)
    assert out["fetched"] == 1 and out["marked"] == 0 and out["abstained"] == 1 and out["exits"] == []
    (note,) = out["row_notes"]
    assert note["kind"] == "abstained" and note["in_exit_window"] is True
    assert note["reason"] == ("the chain fetched this tick is stamped at the tick's start (2026-10-06T10:03:00), "
                              "before this position opened at 2026-10-06T10:03:20; marked on the next tick's fetch")
    assert _events(c, lp.EVENT_MARK_ABSTAINED, "B") == [] and _row(c, "B")["last_mark_ts"] is None
    # that stamp predates the row, so its key is due on the next tick whatever its age —
    # with no fetch possible, it is the cached pre-entry case: named so, and an event in the window
    out = _tick(c, _at(10, 3) + timedelta(seconds=40), E0 + 220, lambda t, x: C1, max_fetches=0)
    assert out["fetched"] == 0 and out["abstained"] == 1
    assert "predates its entry (2026-10-06T10:03:20) and the per-tick fetch cap" in out["row_notes"][0]["reason"]
    assert len(_events(c, lp.EVENT_MARK_ABSTAINED, "B")) == 1
    out = _tick(c, _at(10, 4), E0 + 240, lambda t, x: C1)
    assert out["fetched"] == 1 and out["marked"] == 1 and out["abstained"] == 0


def _armed_at_45(c):
    """Row A armed in the 40% rung (peak 45, lock 0) with a cached chain at
    52.94% — the same rung, so no rung confirmation is involved."""
    _open_live(c, "A", _bull_call(), C0, _at(9, 55))
    c.execute("UPDATE paper_live_positions SET ratchet_peak_pct = 45.0, ratchet_lock_pct = 0.0 "
              "WHERE journal_ref = 'A'")
    c.commit()
    hot = _chain({(24000, "CE"): (180.0, 182.0, 181.0), (24200, "CE"): (50.0, 52.0, 51.0)})   # 128 -> 81/153
    lp._CHAIN_CACHE[("NIFTY 50", EXP)] = (E0, "2026-10-06T10:00:00", hot)


def test_f19_review_a_stale_chain_freezes_the_peak_inside_its_rung_too(world):
    """L4 review: the existing stale test only crosses a rung, where an
    unconfirmed read keeps the peak anyway. Inside a rung a read moves the
    peak by itself — a stale one must not: with the door down the mark is
    recorded, the peak stays 45.0."""
    c = world
    _armed_at_45(c)
    out = _tick(c, _at(10, 31), E0 + 1860, lambda t, x: None)               # the door answers nothing
    assert out["fetched"] == 1 and out["marked"] == 1 and out["stale_marks"] == 1
    a = _row(c, "A")
    assert a["last_mark_ps"] == 128.0 and a["last_capture_pct"] == pytest.approx(52.94, abs=0.01)
    assert (a["ratchet_peak_pct"], a["ratchet_lock_pct"]) == (45.0, 0.0)
    assert a["last_mark_ts"] == "2026-10-06T10:00:00"


@pytest.mark.parametrize("age, fetched, stale", [(300, 1, True), (299, 0, False)])
def test_f19_review_a_chain_exactly_one_quote_interval_old_is_stale(world, age, fetched, stale):
    """The boundary: at one full interval the chain is due — not refreshed,
    it moves nothing; a second younger it is current and the peak rises."""
    c = world
    _armed_at_45(c)
    out = _tick(c, _at(10, 5), E0 + age, lambda t, x: None)
    assert out["fetched"] == fetched and out["marked"] == 1 and out.get("stale_marks") == (1 if stale else None)
    assert _row(c, "A")["ratchet_peak_pct"] == (45.0 if stale else 52.94)


# =================================================================== F20 — the kill switch

KS_ROW = {"ticker": "NIFTY 50", "strategy": "bull_call_spread", "direction": "bullish", "expiry": EXP,
          "entry_mark_ps": 70.0, "max_loss_ps": 70.0, "max_profit_ps": 130.0,
          "ratchet_peak_pct": None, "ratchet_lock_pct": None,
          "legs": [{"side": "BUY", "option_type": "CE", "strike": 24000.0, "entry_fill": 100.0},
                   {"side": "SELL", "option_type": "CE", "strike": 24200.0, "entry_fill": 30.0}]}
KS_CHAIN = _chain({(24000, "CE"): (230.0, 232.0, 231.0), (24200, "CE"): (73.0, 75.0, 74.0)})   # 155: 65.38%


def _ks_entry():
    s = StrategyConstructor(vix=13.0, lot_size=65).construct_bull_call_spread(24000, 24200, 100.0, 30.0)
    s.update(lots=1, expiry=EXP, entry_spot=24000.0)
    return {"short_id": "ks1", "date": "2026-10-06", "ticker": "NIFTY 50", "decision": "approved",
            "outcome": None, "spread": s}


def test_f20_with_the_switch_off_the_live_arm_takes_the_static_65_like_the_primary(monkeypatch):
    """repro test_sf4_ratchet_killswitch_ignored / test_sf_ratchet_killswitch."""
    monkeypatch.setattr("src.config.RATCHET_ENABLED", False)
    today = date(2026, 10, 8)
    assert lb.evaluate_position(_ks_entry(), 24400.0, today)["signal"] == "profit_take"
    assert pt._resolve_spread(_ks_entry(), [("2026-10-07", 24300.0, 24450.0, 24400.0)])[0] == "profit_take"
    ev = lp.evaluate(dict(KS_ROW), KS_CHAIN, today)
    assert ev["ok"] and ev["capture_pct"] == 65.38 and ev["signal"] == "profit_take"
    assert ev["unconfirmed"] is None and (ev["peak"], ev["lock"]) == (None, None)
    # a stored ratchet is left as it is, unused — as the tracker ignores saved rungs
    ev = lp.evaluate(dict(KS_ROW, ratchet_peak_pct=62.0, ratchet_lock_pct=30.0),
                     _chain({(24000, "CE"): (120.0, 122.0, 121.0), (24200, "CE"): (35.0, 37.0, 36.0)}), today)
    assert ev["signal"] == "hold" and (ev["peak"], ev["lock"]) == (62.0, 30.0)     # 15/130: below its old lock


def test_f20_with_the_switch_on_all_three_ratchet(monkeypatch):
    monkeypatch.setattr("src.config.RATCHET_ENABLED", True)
    assert lb.evaluate_position(_ks_entry(), 24400.0, date(2026, 10, 8))["ratchet"] is not None
    ev = lp.evaluate(dict(KS_ROW), KS_CHAIN, date(2026, 10, 8))
    assert ev["signal"] == "hold" and ev["unconfirmed"] == {"peak": 65.38, "lock": 30.0, "capture": 65.38}


def test_f20_one_predicate_rules_all_three_paths(monkeypatch):
    """profit_ratchet.applies is THE gate: the tracker, the live advisory
    and the live arm all follow it (never three copies of the condition)."""
    monkeypatch.setattr("src.config.RATCHET_ENABLED", True)
    monkeypatch.setattr(pr, "applies", lambda spread, max_profit_ps: False)
    today = date(2026, 10, 8)
    assert lb.evaluate_position(_ks_entry(), 24400.0, today)["signal"] == "profit_take"
    assert pt._resolve_spread(_ks_entry(), [("2026-10-07", 24300.0, 24450.0, 24400.0)])[0] == "profit_take"
    assert lp.evaluate(dict(KS_ROW), KS_CHAIN, today)["signal"] == "profit_take"
    monkeypatch.setattr("src.config.RATCHET_ENABLED", False)
    monkeypatch.setattr(pr, "applies", lambda spread, max_profit_ps: True)
    assert lb.evaluate_position(_ks_entry(), 24400.0, today)["ratchet"] is not None
    assert lp.evaluate(dict(KS_ROW), KS_CHAIN, today)["unconfirmed"] is not None


def test_f20_applies_reads_the_switch_the_structure_and_the_profit(monkeypatch):
    monkeypatch.setattr("src.config.RATCHET_ENABLED", True)
    assert pr.applies({"strategy": "bear_put_spread"}, 60.35) is True
    assert pr.applies({"strategy": "iron_condor"}, 40.0) is False
    assert pr.applies({"strategy": "bull_call_spread"}, 0.0) is False
    assert pr.applies({"strategy": "bull_call_spread"}, None) is False
    monkeypatch.setattr("src.config.RATCHET_ENABLED", False)
    assert pr.applies({"strategy": "bear_put_spread"}, 60.35) is False


def test_f20_the_live_arm_takes_profit_end_to_end_with_the_switch_off(world, monkeypatch):
    """repro test_f20_refuter_killswitch_e2e_tick: tick 1 at 65.38% held and
    armed lock 30; tick 2 exited on ratchet_hit, a rule off everywhere else."""
    monkeypatch.setattr("src.config.RATCHET_ENABLED", False)
    c = world
    at = datetime(2026, 9, 29, 11, 0)
    monkeypatch.setattr(lp, "_now", lambda: at)
    s = _quoted(StrategyConstructor(vix=13.0, lot_size=65).construct_bull_call_spread(24000, 24200, 100.0, 30.0), EXP)
    _open_live(c, "ks0001", s, _chain({(24000, "CE"): (98.0, 100.0, 99.0), (24200, "CE"): (30.0, 32.0, 31.0)}), at)
    up = _chain({(24000, "CE"): (175.0, 177.0, 176.0), (24200, "CE"): (18.0, 20.0, 19.0)})
    t = lp.tick(now=at, conn=c, chain_fn=lambda tk, x: up, sleep_fn=lambda s: None,
                now_epoch_fn=lambda: 1000.0, interval_s=300)
    assert [(x["signal"], x["status"]) for x in t["exits"]] == [("profit_take", "settled")]
    tix = [tuple(r) for r in c.execute("SELECT account_id, note FROM trade_tickets WHERE note LIKE 'EXIT%'")]
    assert tix == [(LIVE, "EXIT profit_take")]
    closed = _row(c, "ks0001")
    assert closed["state"] == "closed" and closed["resolution"] == "profit_take"
    assert closed["ratchet_lock_pct"] is None


# =================================================================== F24 — the square-off retry

def _f24_entry(ref="f24v01"):
    spread = {"strategy": "bull_call_spread", "direction": "bullish", "expiry": "2026-10-27",
              "lot_size": 65, "lots": 1, "entry_spot": 24000.0,
              "max_profit": 130.0 * 65, "max_loss": 70.0 * 65, "spread_width": 200.0,
              "legs": [{"side": "BUY", "option_type": "CE", "strike": 24000.0, "premium": 100.0},
                       {"side": "SELL", "option_type": "CE", "strike": 24200.0, "premium": 30.0}]}
    return {"short_id": ref, "ticker": "NIFTY 50", "date": "2026-09-29", "decision": "approved",
            "outcome": None, "spread": spread,
            "ratchet": {"peak_capture_pct": 100.0, "locked_pct": 70.0, "armed": True}}


def _cycles(entries, registry, square_off, minutes, spot=lambda m: 24000.0, notes=None, start=(9, 15)):
    for m in minutes:
        now = datetime(2026, 10, 5, start[0], start[1], 30, tzinfo=IST) + timedelta(minutes=m)
        lb.live_cycle(("NIFTY 50",), quote_fn=lambda u, m=m: {"last_price": spot(m)}, entries=entries,
                      aggregators={}, registry=registry, notify_fn=(notes.append if notes is not None else None),
                      now_fn=lambda now=now: now, square_off_fn=square_off)


def test_f24_a_declined_square_off_is_retried_while_the_signal_persists(monkeypatch, capsys):
    """repro test_f24_verify_square_off_one_shot_real_seam: the REAL seam,
    a persistent registry, 10 cycles with ratchet_hit on 8 of them — the
    quote door was consulted once."""
    monkeypatch.setattr("src.config.LIVE_QUOTE_INTERVAL_SECONDS", 300)   # the default registry reads it
    entries = [_f24_entry()]
    calls = []

    def quotes_fn(entry):
        calls.append(datetime.now())
        return None                                       # the chain door keeps declining

    reg = lb.AlertRegistry()
    notes = []
    _cycles(entries, reg, lambda sig: lb.intraday_square_off(sig, entries=entries, quotes_fn=quotes_fn),
            range(10), spot=lambda m: 24400.0 if m in (4, 5) else 24000.0, notes=notes)
    # 09:15:30 (first sighting) and 09:21:30 (>= one quote interval later, the signal back after
    # the 09:19-09:20 hold) — never in between, and never while the signal is away
    assert len(calls) == 2
    assert len(notes) == 1 and "LIVE exit signal" in notes[0] and "no_chain_quotes" in notes[0]
    assert "square-off retried" not in capsys.readouterr().out       # the same decline: not reprinted


def test_f24_retries_are_paced_per_trade_on_the_quote_interval(monkeypatch):
    monkeypatch.setattr("src.config.LIVE_QUOTE_INTERVAL_SECONDS", 120)
    entries = [_f24_entry("t1"), _f24_entry("t2")]
    tries = []
    _cycles(entries, lb.AlertRegistry(), lambda sig: tries.append(sig["short_id"]) or {"status": "no_chain_quotes"},
            range(7))
    # minutes 0, 2, 4, 6 — each trade on its own clock, once per 120 s
    assert tries == ["t1", "t2"] * 4
    reg = lb.AlertRegistry(retry_s=300)
    tries.clear()
    _cycles(entries[:1], reg, lambda sig: tries.append(sig["short_id"]) or {"status": "no_chain_quotes"}, range(11))
    assert tries == ["t1"] * 3                                         # 0, 5, 10


def test_f24_a_retry_that_fills_squares_off_once_with_one_card(capsys):
    entries = [_f24_entry()]
    answers = iter([{"status": "no_chain_quotes"},
                    {"status": "above_lock_on_real_quotes", "real_capture_pct": 72.0},
                    {"status": "squared_off", "capture_pct": 61.0, "pnl_rs": 1234.5}])
    tries = []

    def square_off(sig):
        tries.append(sig["signal"])
        return next(answers)
    notes = []
    _cycles(entries, lb.AlertRegistry(retry_s=60), square_off, range(6), notes=notes)
    assert tries == ["ratchet_hit"] * 3                              # filled on the 3rd look: never again
    assert len(notes) == 2 and "LIVE exit signal" in notes[0] and notes[1].startswith("✅ **SQUARED OFF")
    out = capsys.readouterr().out
    assert out.count("square-off retried") == 2                       # each CHANGE of outcome, once
    assert "declined (above_lock_on_real_quotes, 72% on real quotes)" in out
    assert "SQUARED OFF intraday on real quotes" in out


def test_f24_an_unchanged_decline_prints_once_and_a_new_one_prints_again(capsys):
    entries = [_f24_entry()]
    seq = ["no_chain_quotes", "missing_leg_quote", "missing_leg_quote", "missing_leg_quote", "no_chain_quotes"]
    it = iter(seq)
    _cycles(entries, lb.AlertRegistry(retry_s=60), lambda sig: {"status": next(it)}, range(5))
    out = capsys.readouterr().out
    assert out.count("square-off retried") == 2                       # missing_leg_quote, then no_chain_quotes
    assert out.count("missing_leg_quote") == 1


def test_f24_alerts_stay_deduplicated_and_offline_callers_stay_read_only():
    entries = [_f24_entry()]
    notes = []
    _cycles(entries, lb.AlertRegistry(retry_s=60), None, range(5), notes=notes)        # no square-off armed
    assert len(notes) == 1
    reg = lb.AlertRegistry()
    sig = {"short_id": "x", "signal": "ratchet_hit"}
    t0 = datetime(2026, 10, 5, 10, 0)
    assert reg.square_off_due(sig, t0) is True
    assert reg.note_square_off(sig, "no_chain_quotes", t0) is True
    assert reg.note_square_off(sig, "no_chain_quotes", t0) is False
    reg.note_square_off(sig, "already_resolved", t0)
    assert reg.square_off_due(sig, t0 + timedelta(hours=1)) is False    # final: never retried


def test_f24_review_a_persisting_non_squarable_signal_never_squares_off(monkeypatch, capsys):
    """L4 review: nothing pinned the RETRY branch's `squarable` gate —
    without it a pre_expiry_exit (advisory only, #69 squares off profit
    takes and ratchet hits) was squared off intraday from its 2nd cycle.
    Five quote intervals of a persisting pre_expiry_exit with the
    square-off ARMED: never called, one advisory card. And with no
    square-off armed (offline callers), a persisting ratchet_hit attempts
    nothing and prints no retry line."""
    monkeypatch.setattr("src.config.LIVE_QUOTE_INTERVAL_SECONDS", 120)
    e = _f24_entry("f24pe01")
    e["spread"]["expiry"] = "2026-10-06"                  # 1 day after the 10-05 cycles: forced-exit window
    e.pop("ratchet")                                      # unarmed: nothing to hit
    assert lb.evaluate_position(e, 24000.0, date(2026, 10, 5))["signal"] == "pre_expiry_exit"
    calls, notes = [], []
    _cycles([e], lb.AlertRegistry(), lambda sig: calls.append(sig["signal"]) or {"status": "no_chain_quotes"},
            range(11), notes=notes)
    assert calls == []
    assert len(notes) == 1 and "LIVE exit signal" in notes[0] and "pre expiry exit at" in notes[0]
    assert "square-off retried" not in capsys.readouterr().out

    attempts = []
    real = lb._attempt_square_off
    monkeypatch.setattr(lb, "_attempt_square_off", lambda sig, fn: attempts.append(sig["signal"]) or real(sig, fn))
    notes.clear()
    _cycles([_f24_entry()], lb.AlertRegistry(), None, range(11), notes=notes)
    assert attempts == []
    assert len(notes) == 1 and "LIVE exit signal" in notes[0] and "ratchet hit at" in notes[0]
    assert "square-off retried" not in capsys.readouterr().out


def test_f24_review_the_doors_quiet_mode_returns_its_reason_and_prints_nothing(monkeypatch, capsys):
    from src import dhan_client
    entry = _f24_entry()
    no_bid = _chain({(24000, "CE"): (0, 112.0, 111.0), (24200, "CE"): (40.0, 42.0, 41.0)})
    good = _chain({(24000, "CE"): (110.0, 112.0, 111.0), (24200, "CE"): (40.0, 42.0, 41.0)})
    monkeypatch.setattr(dhan_client, "get_option_chain", lambda t, x: no_bid)
    assert lb._leg_quotes_for(entry, quiet=True) == (None, "refused on 24000CE: no bid to sell the long leg")
    assert capsys.readouterr().out == ""
    assert lb._leg_quotes_for(entry) is None                                 # the default prints, as before
    assert "(square-off quotes for f24v01: refused on 24000CE: no bid to sell the long leg)" in \
        capsys.readouterr().out
    # the square-off asks quietly and carries the refusal on its result
    out = lb.intraday_square_off({"short_id": "f24v01", "signal": "ratchet_hit"}, entries=[entry])
    assert out == {"status": "no_chain_quotes", "short_id": "f24v01",
                   "reason": "refused on 24000CE: no bid to sell the long leg"}
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(dhan_client, "get_option_chain", lambda t, x: good)
    assert lb._leg_quotes_for(entry, quiet=True) == ({(24000.0, "CE"): 110.0, (24200.0, "CE"): 42.0}, None)


def test_f24_review_a_refusing_door_prints_once_per_change_not_once_per_retry(monkeypatch, capsys):
    """L4 review: the REAL door (only the Dhan call faked) behind the REAL
    square-off seam (its settlement stubbed). The first look reaches real
    quotes and is declined above the lock; then the door refuses on every
    retry. It used to print its refusal line on each one; now its reason
    rides on the bridge's retry line — printed once, on the change."""
    from src import dhan_client
    entries = [_f24_entry()]
    chains = iter([_chain({(24000, "CE"): (110.0, 112.0, 111.0), (24200, "CE"): (40.0, 42.0, 41.0)})])
    asked = []

    def door(ticker, expiry):
        asked.append(expiry)
        return next(chains, None)
    monkeypatch.setattr(dhan_client, "get_option_chain", door)
    monkeypatch.setattr(dhan_client, "last_chain_error", lambda: "rate limit (DH-904: Too many requests)")
    monkeypatch.setattr(pt, "resolve_intraday_profit_take",
                        lambda *a, **k: {"status": "above_lock_on_real_quotes", "real_capture_pct": 72.0})
    reg, fired = lb.AlertRegistry(retry_s=60), []
    for m in range(6):
        now = datetime(2026, 10, 5, 9, 15, 30, tzinfo=IST) + timedelta(minutes=m)
        fired += lb.live_cycle(("NIFTY 50",), quote_fn=lambda u: {"last_price": 24000.0}, entries=entries,
                               aggregators={}, registry=reg, now_fn=lambda now=now: now,
                               square_off_fn=lambda sig: lb.intraday_square_off(sig, entries=entries))
    assert len(asked) == 6 and len(fired) == 1
    assert fired[0]["square_off_status"] == "above_lock_on_real_quotes"
    out = capsys.readouterr().out
    assert "square-off quotes for" not in out                                 # the door said nothing itself
    assert out.count("square-off retried") == 1
    assert ("— intraday fill declined (no_chain_quotes: option chain unavailable (rate limit (DH-904: Too many "
            "requests))); the EOD path owns it.") in out
