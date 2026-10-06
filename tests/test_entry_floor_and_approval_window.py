"""
Chunk 2 Fix F — audit F17 + F18: no entry, and no approval, that the forced
pre-expiry exit would close on arrival.

F17 (medium). The stock-option entry floor equalled the forced exit
(EQUITY_MIN_DAYS_TO_EXPIRY == EQUITY_FORCED_EXIT_DAYS == 7): a stock option
proposed exactly 7 days out was closed by the live arm's first tick and by
the primary's next close — a guaranteed two-crossing round trip. The floor
is now the forced exit + ENTRY_HOLD_BUFFER_DAYS (the index pair's 5-day
gap: MIN_DAYS_TO_EXPIRY 7 vs PRE_EXPIRY_EXIT_DAYS 2), and every underlying
is held to that gap; pick_expiry + physical_settlement_gate are swept over
the NSE calendar against the ONE exit predicate,
plan_tracker.in_forced_exit_window.

F18 (low). A pending entry stays approvable into its forced-exit window
(the tracker resolves pending rows only on a completed daily bar). An
approval there is now refused for EVERY account with status
"inside_exit_window": nothing journaled, no ticket, no live position, no
lock touched, no network call; the D7 sweep releases its locks. Approvals
outside the window are unchanged, and every door into decide_pending (API
bridge, Discord bot, auto-approve, CLI) handles the new status.

Hermetic: ':memory:' brain map, tmp journal (conftest), injected chains and
clocks, Discord muzzled. No network.
"""
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import pytest

from src import brain_map, journal, market_loop, oms, portfolio_manager as pm
from src import options_proposer as op, plan_tracker as pt
from src.execution import live_pricer as lp, paper_venue as pv
from src.strategy import StrategyConstructor

IST = timezone(timedelta(hours=5, minutes=30))
LIVE, ROT, TWO_L = pm.ACCOUNT_PAPER_2L_LIVE, pm.ACCOUNT_PAPER_2L_ROT, pm.ACCOUNT_PAPER_2L
INDEXES = market_loop.INDEX_UNDERLYINGS
STOCKS = tuple(sorted(op.EQUITY_OPTION_UNDERLYINGS))


def _days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# The verified (2026) and provisional (2027) NSE calendar years — every day,
# weekends and holidays included, so a holiday-moved expiry is covered too.
SWEEP = list(_days(date(2026, 1, 1), date(2027, 12, 31)))


# ======================================================== F17: the constants

def test_the_stock_floor_is_the_forced_exit_plus_the_holding_window():
    assert op.ENTRY_HOLD_BUFFER_DAYS == 5
    # the forced exit guards physical settlement and did NOT move
    assert op.EQUITY_FORCED_EXIT_DAYS == 7
    assert op.EQUITY_MIN_DAYS_TO_EXPIRY == op.EQUITY_FORCED_EXIT_DAYS + op.ENTRY_HOLD_BUFFER_DAYS == 12
    # the index pair already had exactly this gap; it is the model, unchanged
    assert op.MIN_DAYS_TO_EXPIRY == 7 and pt.PRE_EXPIRY_EXIT_DAYS == 2


@pytest.mark.parametrize("underlying", INDEXES + STOCKS)
def test_every_underlying_keeps_the_holding_window_between_entry_and_forced_exit(underlying):
    """INVARIANT (index AND equity): entry floor - forced exit >= the buffer."""
    gap = op.min_days_to_expiry_for(underlying) - op.forced_exit_days_for(underlying)
    assert gap >= op.ENTRY_HOLD_BUFFER_DAYS, (underlying, gap)
    # and the tracker's own lookup agrees with the proposer's
    assert pt._forced_exit_days(underlying) == op.forced_exit_days_for(underlying)


def test_the_raw_pairs_hold_the_invariant_too():
    assert op.MIN_DAYS_TO_EXPIRY - pt.PRE_EXPIRY_EXIT_DAYS >= op.ENTRY_HOLD_BUFFER_DAYS
    assert op.EQUITY_MIN_DAYS_TO_EXPIRY - op.EQUITY_FORCED_EXIT_DAYS >= op.ENTRY_HOLD_BUFFER_DAYS


# ================================== F17: the sweep against the exit predicate

@pytest.mark.parametrize("underlying", INDEXES + STOCKS)
def test_pick_expiry_never_chooses_an_expiry_already_inside_the_forced_exit_window(underlying):
    """Every day of 2026-2027 against a DENSE ladder (an expiry on every
    calendar day), so the short horizon lands exactly on the floor — the
    tightest choice pick_expiry can ever make. The chosen expiry must clear
    the physical-settlement gate and sit OUTSIDE in_forced_exit_window —
    both rules of it: the calendar-day rule and the last-session rule."""
    for today in SWEEP:
        ladder = [(today + timedelta(days=k)).isoformat() for k in range(0, 40)]
        for horizon in ("short", "long"):
            chosen = op.pick_expiry(ladder, today=today, underlying=underlying, horizon=horizon)
            assert chosen is not None, (underlying, today, horizon)
            assert op.physical_settlement_gate(underlying, chosen, today=today) == (True, None)
            assert not pt.in_forced_exit_window(underlying, chosen, today), (underlying, today, chosen)
        # the short pick is the floor itself, never closer
        assert (date.fromisoformat(op.pick_expiry(ladder, today=today, underlying=underlying))
                - today).days == op.min_days_to_expiry_for(underlying)


@pytest.mark.parametrize("underlying", STOCKS)
def test_the_settlement_gate_never_admits_a_stock_expiry_inside_the_forced_exit_window(underlying):
    """An INJECTED expiry (build_proposal's `expiry=`) meets only the gate.
    For a stock option, anything the gate admits is outside the window. (An
    index is cash-settled and the gate passes it unconditionally by design;
    its entry clock is pick_expiry's floor, swept above.)"""
    for today in SWEEP:
        for d in range(0, 31):
            expiry = (today + timedelta(days=d)).isoformat()
            if op.physical_settlement_gate(underlying, expiry, today=today)[0]:
                assert d >= op.EQUITY_MIN_DAYS_TO_EXPIRY
                assert not pt.in_forced_exit_window(underlying, expiry, today), (today, expiry)


# ==================================== F17: the boundary day, through the desk

NEAR, NEXT = "2026-12-29", "2027-01-26"        # ICICIBANK monthlies (the audit's trigger)


def _icici_chain():
    oc = {}
    for k in range(1300, 1501, 20):
        oc[f"{float(k):.6f}"] = {
            "ce": {"top_bid_price": 10.0, "top_ask_price": 11.0, "last_price": 10.5},
            "pe": {"top_bid_price": 10.0, "top_ask_price": 11.0, "last_price": 10.5}}
    oc[f"{1400.0:.6f}"]["pe"] = {"top_bid_price": 30.0, "top_ask_price": 31.0, "last_price": 30.5}
    oc[f"{1320.0:.6f}"]["pe"] = {"top_bid_price": 3.0, "top_ask_price": 4.0, "last_price": 3.5}
    return {"last_price": 1400.0, "oc": oc}


@pytest.fixture
def desk(monkeypatch):
    import src.adaptive_sizing as asz
    ladder = {"v": [NEAR, NEXT]}
    monkeypatch.setattr(op, "get_expiry_list", lambda u: list(ladder["v"]))
    monkeypatch.setattr(op, "get_option_chain", lambda u, e: pytest.fail("the chain is injected"))
    monkeypatch.setattr(asz, "adjust_option_lots", lambda strategy, lots, *a, **k: (lots, None))
    return ladder


def _propose(today, expiry=None):
    analysis = {"uptrend": False, "fresh_cross": False, "rsi": 50.0, "price": 1400.0, "closes": None}
    return op.build_proposal("ICICIBANK.NS", analysis=analysis, vix=13.0, chain=_icici_chain(),
                             book={"cash": 1_000_000.0, "positions": {}}, prices={},
                             account_equity=1_000_000.0, advisory=None, today=today,
                             expiry=expiry)


def test_a_day7_stock_option_is_no_longer_proposed_on_the_near_month(desk):
    """The audit's trigger: Tue 2026-12-22, exactly 7 days before the
    12-29 stock expiry. Before: the near month was proposed and allowed, and
    the first tick closed it. Now the desk reaches for the next month."""
    r = _propose(date(2026, 12, 22))
    assert r["reason"] == "ok", r["reason"]
    assert r["proposal"]["spread"]["expiry"] == NEXT
    # with only the near month listed there is nothing usable — no proposal
    desk["v"] = [NEAR]
    r = _propose(date(2026, 12, 22))
    assert r["proposal"] is None
    assert r["reason"] == "no usable expiry (need >= 12 days out)"


def test_an_injected_day7_stock_expiry_is_refused_by_the_gate(desk):
    r = _propose(date(2026, 12, 22), expiry=NEAR)
    assert r["proposal"] is None
    assert "PHYSICAL SETTLEMENT GATE: 7d to expiry, minimum 12d" in r["reason"]
    assert "audit F17" in r["reason"]


def test_the_new_floor_day_still_takes_the_near_month(desk):
    """Day 12 is the first day the near month is allowed; day 11 is not."""
    assert _propose(date(2026, 12, 17))["proposal"]["spread"]["expiry"] == NEAR      # 12d
    assert _propose(date(2026, 12, 18))["proposal"]["spread"]["expiry"] == NEXT      # 11d


# =========================================== F18: the approval-time refusal

class _KeepOpen:
    """The shared ':memory:' connection survives every callee's close()."""

    def __init__(self, inner):
        self._c = inner

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._c, name)


def _book(q):
    oc = {}
    for (k, t), (bid, ask, ltp) in q.items():
        oc.setdefault(f"{float(k):.6f}", {})[t.lower()] = {"top_bid_price": bid, "top_ask_price": ask,
                                                            "last_price": ltp}
    return {"last_price": 24000.0, "oc": oc}


BOOK = _book({(24000, "CE"): (96.0, 100.0, 98.0), (24200, "CE"): (30.0, 34.0, 32.0)})


@pytest.fixture
def world(monkeypatch):
    """Primary + 2L + ROT + LIVE all on, the venue on, the market open."""
    raw = brain_map.connect(":memory:")
    c = _KeepOpen(raw)
    pm.ensure_accounts_schema(c)
    oms.ensure_schema(c)
    lp.ensure_schema(c)
    pm.get_account(c)
    monkeypatch.setattr(brain_map, "connect", lambda *a, **k: c)
    monkeypatch.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", True)
    monkeypatch.setattr("src.config.PAPER_VENUE_ENABLED", True)
    monkeypatch.setattr(pv, "_tier_frac", lambda u, slippage_fn=None: 0.0)
    monkeypatch.setattr(lp, "_market_open", lambda now: True)
    monkeypatch.setattr(lp, "_stamp_journal", lambda row, payload: None)
    monkeypatch.setattr(op, "_PAPER_VENUE_KEEP_CONN", True)
    chain_calls = []
    monkeypatch.setattr(op, "_LIVE_CHAIN_FN", lambda t, x: chain_calls.append((t, x)) or BOOK)
    notes = []
    monkeypatch.setattr(op, "_notify_discord", lambda *a, **k: notes.append(a))
    monkeypatch.setattr("src.notifier.fire_broadcast", lambda *a, **k: notes.append(a))
    lp.reset_cache()
    yield {"c": c, "chain_calls": chain_calls, "notes": notes, "mp": monkeypatch}
    lp.reset_cache()
    raw.close()


def _approve_on(w, day: date, ref: str, approve=True):
    w["mp"].setattr(op, "_today", lambda: day)
    w["mp"].setattr(lp, "_now", lambda: datetime(day.year, day.month, day.day, 10, 0, tzinfo=IST))
    return op.decide_pending(ref, approve=approve, why="late tap", human=False)


def _propose_pending(c, ref, expiry, ticker="NIFTY 50", proposed="2026-10-20"):
    """A pending spread exactly as run_headless leaves it: the primary's
    lock taken by the headless gate, each shadow account judged (and locked)
    at proposal time, the verdicts stamped on the journaled row."""
    s = StrategyConstructor(vix=13.0, lot_size=65).construct_bull_call_spread(24000, 24200, 100.0, 30.0)
    for leg in s["legs"]:
        leg["fill_basis"] = "quoted"
    s.update(lots=1, expiry=expiry, entry_spot=24000.0)
    required = pm.required_margin_for({"spread": s, "vix": 13.0})
    assert pm.gate_headless_entry(ref, required)[0]
    accounts = op._judge_shadow_accounts(ref, {"spread": s, "lots": 1, "vix": 13.0})
    row = {"short_id": ref, "date": proposed, "ticker": ticker, "action": "SPREAD",
           "decision": "pending_approval", "why": "(headless proposal)", "outcome": None,
           "spread": s, "signal": "t", "receipt": {"vix": 13.0}, "accounts": accounts}
    journal.log(row)
    return row


def _active_locks(c, ref):
    prim = c.execute("SELECT COUNT(*) FROM margin_locks WHERE journal_ref = ? AND released_at IS NULL",
                     (ref,)).fetchone()[0]
    shadows = sorted(r[0] for r in c.execute(
        "SELECT account_id FROM paper_margin_locks WHERE journal_ref = ? AND released_at IS NULL",
        (ref,)).fetchall())
    return prim, shadows


def test_a_late_approval_inside_the_window_is_refused_for_every_account(world):
    """The audit's trigger: a NIFTY 50 bull call proposed 10-20 for the Tue
    10-27 expiry, approved Mon 10-26 at 10:00 (1 day left <= 2)."""
    c = world["c"]
    row = _propose_pending(c, "f18late1", "2026-10-27")
    assert set(row["accounts"]) == {TWO_L, ROT, LIVE}
    assert all(v["status"] == "approved" for v in row["accounts"].values())
    assert _active_locks(c, "f18late1") == (1, sorted([TWO_L, ROT, LIVE]))
    writes_before = c.total_changes
    world["notes"].clear()

    v = _approve_on(world, date(2026, 10, 26), "f18late1")

    assert v["status"] == op.INSIDE_EXIT_WINDOW == "inside_exit_window"
    assert "1d to the 2026-10-27 expiry" in v["reason"] and "audit F18" in v["reason"]
    # nothing journaled: the row is byte-for-byte the pending proposal
    assert journal.get_entry("f18late1") == row
    # nothing written to the books at all: no ticket, no position, no lock
    # re-taken or released, no event
    assert c.total_changes == writes_before
    assert c.execute("SELECT COUNT(*) FROM trade_tickets").fetchone()[0] == 0
    assert not lp.has_open_position(c, LIVE, "f18late1")
    assert _active_locks(c, "f18late1") == (1, sorted([TWO_L, ROT, LIVE]))
    # the live arm never re-quoted (no Dhan call, before or inside the lock),
    # and no card or broadcast went out
    assert world["chain_calls"] == []
    assert world["notes"] == []
    # the existing D7 15:30 sweep releases every lock the entry holds
    freed = pm.expire_pending_lock(c, "f18late1")
    assert set(freed) == {pm.ACCOUNT_PAPER_10L, TWO_L, ROT, LIVE}
    assert _active_locks(c, "f18late1") == (0, [])


def test_an_approval_outside_the_window_is_unchanged(world):
    """Wed 10-21, 6 days before the same expiry: approved for every account,
    tickets issued, the live arm re-quotes and opens its position."""
    c = world["c"]
    world["mp"].setattr(pm, "CAPITAL_ROTATION_ENABLED", False)   # rotation's own file covers its approval
    _propose_pending(c, "f18ok001", "2026-10-27")
    v = _approve_on(world, date(2026, 10, 21), "f18ok001")
    assert v["status"] == "approved"
    e = journal.get_entry("f18ok001")
    assert e["decision"] == "approved" and e["execution"]["mode"] == "paper_venue"
    assert e["accounts"][LIVE]["status"] == "approved"
    assert lp.has_open_position(c, LIVE, "f18ok001")
    assert world["chain_calls"]                                   # the live re-quote ran
    assert c.execute("SELECT COUNT(*) FROM trade_tickets").fetchone()[0] == 3   # 10L, 2L, LIVE


def test_the_last_session_before_a_holiday_moved_expiry_is_inside_the_window(world):
    """Rule 2 of in_forced_exit_window: the NIFTY weekly of Mon 2026-10-19
    (Dussehra Tue 10-20). Fri 10-16 is 3 calendar days out — outside the
    2-day rule, but the LAST session before expiry, where both the primary
    and the live arm exit. The approval gate asks the same predicate."""
    c = world["c"]
    world["mp"].setattr(pm, "CAPITAL_ROTATION_ENABLED", False)
    _propose_pending(c, "f18mon01", "2026-10-19", proposed="2026-10-09")
    v = _approve_on(world, date(2026, 10, 16), "f18mon01")
    assert v["status"] == "inside_exit_window"
    assert "3d to the 2026-10-19 expiry" in v["reason"]
    # Thu 10-15 is not the last session (Fri 10-16 still is): approvable
    assert _approve_on(world, date(2026, 10, 15), "f18mon01")["status"] == "approved"


def test_a_stock_option_is_refused_inside_its_7_day_window(world):
    c = world["c"]
    world["mp"].setattr(pm, "CAPITAL_ROTATION_ENABLED", False)
    world["mp"].setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", False)
    _propose_pending(c, "f18stk01", NEAR, ticker="ICICIBANK.NS", proposed="2026-12-14")
    v = _approve_on(world, date(2026, 12, 22), "f18stk01")
    assert v["status"] == "inside_exit_window"
    assert "7d to the 2026-12-29 expiry" in v["reason"] and "(7d," in v["reason"]
    assert journal.get_entry("f18stk01")["decision"] == "pending_approval"
    # one day earlier (8 days) it is outside the stock window
    assert _approve_on(world, date(2026, 12, 21), "f18stk01")["status"] == "approved"


def test_a_rejection_inside_the_window_is_never_refused(world):
    c = world["c"]
    world["mp"].setattr(pm, "CAPITAL_ROTATION_ENABLED", False)
    _propose_pending(c, "f18rej01", "2026-10-27")
    v = _approve_on(world, date(2026, 10, 26), "f18rej01", approve=False)
    assert v["status"] == "rejected"
    assert journal.get_entry("f18rej01")["decision"] == "rejected"
    assert _active_locks(c, "f18rej01") == (0, [])                # released at zero


def test_the_refusal_reason_names_the_case_and_fails_closed_on_an_unreadable_expiry():
    today = date(2026, 10, 26)
    assert op._exit_window_refusal({"ticker": "NIFTY 50", "spread": {"expiry": "2026-11-24"}}, today) is None
    assert "inside the forced pre-expiry exit window (2d" in op._exit_window_refusal(
        {"ticker": "NIFTY 50", "spread": {"expiry": "2026-10-27"}}, today)
    # an expired contract is inside the window too
    assert op._exit_window_refusal({"ticker": "NIFTY 50", "spread": {"expiry": "2026-10-20"}}, today)
    # no spread / no expiry: not an option spread, no window to be inside
    assert op._exit_window_refusal({"ticker": "INFY.NS"}, today) is None
    assert op._exit_window_refusal({"ticker": "NIFTY 50", "spread": {"legs": []}}, today) is None
    # an expiry that cannot be read is refused, never guessed
    for bad in ("garbage", "2026-13-45", ["2026-11-24"]):
        why = op._exit_window_refusal({"ticker": "NIFTY 50", "spread": {"expiry": bad}}, today)
        assert why and "fail-closed" in why, bad


def test_the_approval_clock_is_the_ist_date():
    """23:00 UTC on 10-25 is 04:30 IST on 10-26 — the exchange's day."""
    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 25, 23, 0, tzinfo=timezone.utc).astimezone(tz)
    with mock.patch.object(op, "datetime", _Clock):
        assert op._today() == date(2026, 10, 26)


# ====================================== F18: every door handles the status

def _pending_api_row():
    return {"short_id": "api00001", "date": "2026-10-20", "action": "SPREAD", "ticker": "NIFTY 50",
            "shares": 65, "price": 70.0, "signal": "t", "decision": "pending_approval",
            "why": "(headless proposal)", "outcome": None,
            "spread": {"strategy": "bull_call_spread", "expiry": "2026-10-27",
                       "lot_size": 65, "lots": 1, "legs": []}}


def test_the_api_bridge_answers_409_inside_the_window_and_writes_nothing(monkeypatch):
    from fastapi.testclient import TestClient
    from src.api_server import app
    entry = _pending_api_row()
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setattr(op, "_today", lambda: date(2026, 10, 26))
    monkeypatch.setattr(op.journal, "read_all", lambda: [entry])
    rewrite = mock.Mock()
    monkeypatch.setattr(op.journal, "rewrite_all", rewrite)
    monkeypatch.setattr(op, "_notify_discord", lambda *a, **k: pytest.fail("no card for a refusal"))
    r = TestClient(app).post("/api/discord/action", headers={"X-API-Key": "k"},
                             json={"action": "approve", "trade_id": "api00001", "why": "late"})
    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False and body["status"] == "inside_exit_window"
    assert "forced pre-expiry exit window" in body["error"] and body["trade_id"] == "api00001"
    assert not rewrite.called and entry["decision"] == "pending_approval"


def test_the_discord_bot_says_too_late_and_keeps_the_buttons():
    from src import discord_bot as bot
    note, retire = bot._decision_note("api00001", 409, {"ok": False, "status": "inside_exit_window",
                                                        "error": "1d to the 2026-10-27 expiry ..."})
    assert retire is False                         # still pending: Reject still works
    assert "Too late to approve `api00001`" in note and "1d to the 2026-10-27 expiry" in note
    assert "journaled. " not in note
    # the other answers are exactly what they were
    note, retire = bot._decision_note("x", 409, {"ok": False, "error": "resolved"})
    assert retire is True and "already resolved" in note
    note, retire = bot._decision_note("x", 200, {"ok": True, "decision": "approved"})
    assert retire is True and note.startswith("✅ **APPROVED**")
    note, retire = bot._decision_note("x", 200, {"ok": True, "decision": "rejected"})
    assert retire is True and note.startswith("❌ **REJECTED**")
    note, retire = bot._decision_note("x", 404, {})
    assert retire is True and "no pending entry" in note
    note, retire = bot._decision_note("x", 503, {"error": "locked"})
    assert retire is False and "HTTP 503: locked" in note


def test_auto_approve_reports_the_refusal_and_leaves_the_entry_pending(monkeypatch):
    """Unreachable from today's proposer (the floor keeps every fresh
    proposal outside the window), but the door must not crash if it is."""
    canned = {"ticker": "NIFTY 50", "action": "SPREAD", "shares": 65, "price": 70.0, "lots": 1,
              "signal": "t", "view": "bullish", "vix": 13.0,
              "spread": dict(_pending_api_row()["spread"], max_loss=4550.0, max_profit=8450.0,
                             net_debit=70.0, margin={"total_margin": 17550.0})}
    monkeypatch.setenv(op.AUTO_APPROVE_ENV_KEY, "1")
    monkeypatch.setattr(op, "_today", lambda: date(2026, 10, 26))
    monkeypatch.setattr(op, "build_proposal", lambda *a, **k: {"proposal": canned, "reason": "ok"})
    for name in ("_memory_context_for", "_skeptic_note_for"):
        monkeypatch.setattr(op, name, lambda *a, **k: "")
    monkeypatch.setattr(op, "_format_proposal_alert", lambda p, action_note="": "alert")
    monkeypatch.setattr(op, "_notify_discord", lambda *a, **k: None)
    monkeypatch.setattr(op, "_judge_shadow_accounts", lambda *a, **k: {})
    monkeypatch.setattr("src.exposure_gate.gate_entry", lambda *a, **k: (True, None))
    monkeypatch.setattr(pm, "gate_headless_entry", lambda *a, **k: (True, "locked"))
    from src import human_pulse
    monkeypatch.setattr(human_pulse, "auto_approve_tripped", lambda *a, **k: False)
    res = op.run_headless("NIFTY 50", state={})
    assert res["proposed"] is True and res["auto_approved"] is False
    assert "forced pre-expiry exit window" in res["reason"]
    assert journal.get_entry(res["entry"]["short_id"])["decision"] == "pending_approval"


def test_the_cli_names_the_refusal_and_counts_nothing_decided(monkeypatch, capsys):
    entry = _pending_api_row()
    entry["spread"].update(net_debit=70.0, max_loss=4550.0, max_profit=8450.0)
    journal.log(entry)
    monkeypatch.setattr(op, "_today", lambda: date(2026, 10, 26))
    monkeypatch.setattr(op, "_notify_discord", lambda *a, **k: None)
    monkeypatch.setattr("builtins.input", mock.Mock(side_effect=["y", "late"]))
    assert op.review_pending() == 0
    assert "not decided: inside_exit_window (" in capsys.readouterr().out
    assert journal.get_entry("api00001")["decision"] == "pending_approval"
