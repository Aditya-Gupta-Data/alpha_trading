"""
Chunk 2 Fix G — the #68 one-position-per-underlying+direction slot is
re-checked at APPROVAL (Architect ruling, 2026-10-06).

Before: exposure_gate.gate_entry ran only in run_headless's proposal path,
and it never counts PENDING rows. So two pending entries on one
underlying+direction could both be approved, and a PAPER_2L_LIVE position
opened while an entry waited never stopped its approval.

Now `_decide_pending_locked` asks `exposure_gate.gate_approval` under the
journal lock, on the fresh row, AFTER the free F18 window check and BEFORE
margin / the shadow accounts / the venue / the journal. A conflict anywhere
in the firm returns status "exposure_blocked": the entry stays pending,
nothing is journaled, no ticket, no live position, no lock re-taken, no
broadcast, no opportunity-cost row. There is one `stage: "approval"` ledger
line per entry per day, and an AUTO-approval's block gets one Discord card.
The entry's own ref never blocks itself. An unreadable firm view fails
open. A rejection is never blocked. Every door handles the status.

Also closes the two F-review test gaps for BOTH approval refusals
(inside_exit_window and exposure_blocked):
  (a) the D7 15:30 sweep has already expired the entry's locks, and no lock
      is re-taken;
  (b) neither network prefetch (_prefetch_live_requote, _prefetch_rotation)
      runs on a refused approval.

Hermetic: a FILE brain_map at the per-test tmp DEFAULT_DB_PATH (the gate's
production read opens it `mode=ro`; every writer shares one connection),
tmp journal (conftest), tmp block ledger, injected chains and clocks,
Discord muzzled. No network.
"""
import json
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import pytest

from src import brain_map, journal, oms, portfolio_manager as pm
from src import exposure_gate as eg, options_proposer as op
from src.execution import live_pricer as lp, paper_venue as pv
from src.strategy import StrategyConstructor

IST = timezone(timedelta(hours=5, minutes=30))
PRIMARY, TWO_L = pm.ACCOUNT_PAPER_10L, pm.ACCOUNT_PAPER_2L
ROT, LIVE = pm.ACCOUNT_PAPER_2L_ROT, pm.ACCOUNT_PAPER_2L_LIVE
EXPIRY = "2026-10-27"                       # NIFTY 50 weekly (Tue)
DAY = date(2026, 10, 21)                    # 6 days out: outside the 2-day window
LATE = date(2026, 10, 26)                   # 1 day out: inside it (F18)


class _KeepOpen:
    """The shared file connection survives every callee's close()."""

    def __init__(self, c):
        object.__setattr__(self, "_c", c)

    def __getattr__(self, n):
        return getattr(self._c, n)

    def __setattr__(self, n, v):
        setattr(self._c, n, v)

    def close(self):
        pass


def _book(q):
    oc = {}
    for (k, t), (bid, ask, ltp) in q.items():
        oc.setdefault(f"{float(k):.6f}", {})[t.lower()] = {
            "top_bid_price": bid, "top_ask_price": ask, "last_price": ltp}
    return {"last_price": 24000.0, "oc": oc}


BOOK = _book({(24000, "CE"): (96.0, 100.0, 98.0), (24200, "CE"): (30.0, 34.0, 32.0),
              (24000, "PE"): (96.0, 100.0, 98.0), (23800, "PE"): (30.0, 34.0, 32.0)})


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Primary + 2L + ROT + LIVE on, the venue on, the market open; the
    gate reads the same file every writer shares."""
    raw = brain_map.connect(brain_map.DEFAULT_DB_PATH)
    c = _KeepOpen(raw)
    pm.ensure_accounts_schema(c)
    oms.ensure_schema(c)
    lp.ensure_schema(c)
    pm.get_account(c)
    mp = monkeypatch
    mp.setattr(brain_map, "connect", lambda *a, **k: c)
    mp.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", True)
    mp.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", True)
    mp.setattr(pm, "CAPITAL_ROTATION_ENABLED", True)
    mp.setattr("src.config.PAPER_VENUE_ENABLED", True)
    mp.setattr(pv, "_tier_frac", lambda u, slippage_fn=None: 0.0)
    mp.setattr(lp, "_market_open", lambda now: True)
    mp.setattr(lp, "_stamp_journal", lambda row, payload: None)
    mp.setattr(op, "_PAPER_VENUE_KEEP_CONN", True)
    chain_calls = []
    mp.setattr(op, "_LIVE_CHAIN_FN", lambda t, x: chain_calls.append((t, x)) or BOOK)
    # Both network prefetches are spied. The live re-quote runs for real
    # (the chain is injected). Rotation returns no data, so no eviction:
    # the rotation arm's own file covers its approval.
    prefetch = {"live": [], "rotation": []}
    real_live = op._prefetch_live_requote
    mp.setattr(op, "_prefetch_live_requote",
               lambda ref: prefetch["live"].append(ref) or real_live(ref))
    mp.setattr(op, "_prefetch_rotation",
               lambda ref: prefetch["rotation"].append(ref) or None)
    notes, broadcasts = [], []
    mp.setattr(op, "_notify_discord", lambda text, *a, **k: notes.append(text))
    mp.setattr("src.notifier.fire_broadcast", lambda *a, **k: broadcasts.append(a))
    mp.setattr(eg, "LEDGER_PATH", tmp_path / "exposure_blocks.jsonl")
    opp = []
    mp.setattr(eg, "_record_opportunity_cost", lambda *a, **k: opp.append((a, k)))
    lp.reset_cache()
    yield {"c": c, "mp": mp, "chain_calls": chain_calls, "prefetch": prefetch,
           "notes": notes, "broadcasts": broadcasts, "opp": opp, "tmp": tmp_path}
    lp.reset_cache()
    raw.close()


def _spread(direction="bullish", expiry=EXPIRY):
    sc = StrategyConstructor(vix=13.0, lot_size=65)
    s = (sc.construct_bull_call_spread(24000, 24200, 100.0, 30.0) if direction == "bullish"
         else sc.construct_bear_put_spread(24000, 23800, 100.0, 30.0))
    assert s["direction"] == direction
    for leg in s["legs"]:
        leg["fill_basis"] = "quoted"
    s.update(lots=1, expiry=expiry, entry_spot=24000.0)
    return s


def _propose_pending(ref, direction="bullish", ticker="NIFTY 50", expiry=EXPIRY,
                     proposed="2026-10-20"):
    """A pending spread exactly as run_headless leaves it: the primary's
    lock taken by the headless gate, each shadow account judged (and
    locked) at proposal time, the verdicts stamped on the journaled row."""
    s = _spread(direction, expiry)
    required = pm.required_margin_for({"spread": s, "vix": 13.0})
    assert pm.gate_headless_entry(ref, required)[0]
    accounts = op._judge_shadow_accounts(ref, {"spread": s, "lots": 1, "vix": 13.0})
    row = {"short_id": ref, "date": proposed, "ticker": ticker, "action": "SPREAD",
           "decision": "pending_approval", "why": "(headless proposal)", "outcome": None,
           "spread": s, "signal": "t", "receipt": {"vix": 13.0}, "accounts": accounts,
           "regime": {"trend": direction}}
    journal.log(row)
    return row


def _approve(w, ref, day=DAY, approve=True, human=True):
    w["mp"].setattr(op, "_today", lambda: day)
    w["mp"].setattr(lp, "_now", lambda: datetime(day.year, day.month, day.day, 10, 0, tzinfo=IST))
    return op.decide_pending(ref, approve=approve, why="tap", human=human)


def _active_locks(c, ref):
    prim = c.execute("SELECT COUNT(*) FROM margin_locks WHERE journal_ref = ? AND released_at IS NULL",
                     (ref,)).fetchone()[0]
    shadows = sorted(r[0] for r in c.execute(
        "SELECT account_id FROM paper_margin_locks WHERE journal_ref = ? AND released_at IS NULL",
        (ref,)).fetchall())
    return prim, shadows


def _tickets(c, ref):
    return c.execute("SELECT COUNT(*) FROM trade_tickets WHERE journal_ref = ?", (ref,)).fetchone()[0]


def _ledger(w):
    path = eg.LEDGER_PATH
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def _snapshot(c):
    return (c.total_changes,
            c.execute("SELECT COUNT(*) FROM paper_margin_locks").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM margin_locks").fetchone()[0])


def _settle_primary_only(c, ref):
    """X's primary settles while LIVE keeps its own position (#120, the F09
    shape): the journal row gets its outcome, and release_entry settles the
    primary + 2L + ROT and KEEPS LIVE's lock (has_open_position)."""
    journal.update_entry(ref, lambda e: e.update(outcome={"resolution": "ratchet_hit", "pnl_rs": 0.0}))
    pm.release_entry(ref, 0.0, conn=c, wealth_sweep=False)
    assert lp.has_open_position(c, LIVE, ref)
    assert _active_locks(c, ref) == (0, [LIVE])


# ============================== (i) two pending entries on one slot

def test_two_pending_entries_on_one_slot_only_the_first_approval_opens(world):
    c = world["c"]
    _propose_pending("aaaa0001")
    pend_b = _propose_pending("bbbb0002")       # the proposal gate never counts pending rows
    assert _active_locks(c, "bbbb0002") == (1, sorted([TWO_L, ROT, LIVE]))

    assert _approve(world, "aaaa0001")["status"] == "approved"
    assert lp.has_open_position(c, LIVE, "aaaa0001")
    world["notes"].clear(), world["broadcasts"].clear(), world["chain_calls"].clear()
    world["prefetch"]["live"].clear(), world["prefetch"]["rotation"].clear()
    before = _snapshot(c)

    v = _approve(world, "bbbb0002")

    assert v["status"] == op.EXPOSURE_BLOCKED == "exposure_blocked"
    assert "announce" not in v                              # internal, never leaks to a door
    assert v["reason"] == (
        "exposure gate: 1 open bullish position(s) on NIFTY 50 already "
        f"(`aaaa0001` {PRIMARY}, {TWO_L}, {ROT}, {LIVE}) — max one per "
        "underlying+direction, firm-wide, re-checked at approval (decision #68)")
    # left PENDING, byte-for-byte; nothing written to the books at all
    assert journal.get_entry("bbbb0002") == pend_b
    assert _snapshot(c) == before
    assert _tickets(c, "bbbb0002") == 0
    assert not lp.has_open_position(c, LIVE, "bbbb0002")
    assert _active_locks(c, "bbbb0002") == (1, sorted([TWO_L, ROT, LIVE]))   # D7 releases them
    # no network, no card for a human tap, no broadcast, no opportunity cost
    assert world["chain_calls"] == [] and world["prefetch"] == {"live": [], "rotation": []}
    assert world["notes"] == [] and world["broadcasts"] == []
    assert world["opp"] == []
    # ONE approval-stage ledger line naming the entry and the holder
    (rec,) = _ledger(world)
    assert rec["stage"] == eg.APPROVAL_STAGE == "approval"
    assert rec["trade_id"] == "bbbb0002" and rec["ts"].startswith(DAY.isoformat())
    assert rec["ticker"] == "NIFTY 50" and rec["direction"] == "bullish"
    assert rec["blocked_by"] == ["aaaa0001"]
    assert rec["held_by"] == {"aaaa0001": [PRIMARY, TWO_L, ROT, LIVE]}
    # the 15:30 sweep releases every lock the blocked entry still holds
    assert set(pm.expire_pending_lock(c, "bbbb0002")) == {PRIMARY, TWO_L, ROT, LIVE}


def test_a_retried_blocked_approval_writes_one_line_per_entry_per_day(world):
    _propose_pending("aaaa0001")
    _propose_pending("bbbb0002")
    _propose_pending("cccc0003")
    assert _approve(world, "aaaa0001")["status"] == "approved"
    for _ in range(3):
        assert _approve(world, "bbbb0002")["status"] == "exposure_blocked"
    assert _approve(world, "cccc0003")["status"] == "exposure_blocked"
    # the next day is a new day (still outside the window)
    assert _approve(world, "bbbb0002", day=DAY + timedelta(days=1))["status"] == "exposure_blocked"
    assert [(r["trade_id"], r["ts"][:10]) for r in _ledger(world)] == [
        ("bbbb0002", "2026-10-21"), ("cccc0003", "2026-10-21"), ("bbbb0002", "2026-10-22")]
    assert world["notes"] == [n for n in world["notes"] if "Exposure gate" not in n]


# ======================= (ii) a PAPER_2L_LIVE position opened while it waited

def test_a_live_position_opened_while_the_entry_waited_blocks_and_names_live(world):
    """P waits. X is proposed after it (the gate never counts pending P),
    approved, and LIVE opens X; X's primary then settles while LIVE keeps
    its position. P's approval must see LIVE's X."""
    c = world["c"]
    pend = _propose_pending("pppp0001")
    _propose_pending("xxxx0001")
    assert _approve(world, "xxxx0001")["status"] == "approved"
    _settle_primary_only(c, "xxxx0001")
    before = _snapshot(c)

    v = _approve(world, "pppp0001")

    assert v["status"] == "exposure_blocked"
    assert f"(`xxxx0001` {LIVE})" in v["reason"] and PRIMARY not in v["reason"]
    assert journal.get_entry("pppp0001") == pend and _snapshot(c) == before
    assert not lp.has_open_position(c, LIVE, "pppp0001") and _tickets(c, "pppp0001") == 0
    assert _ledger(world)[-1]["held_by"] == {"xxxx0001": [LIVE]}
    # once LIVE exits on its own quotes, the slot is free and P approves
    row = next(r for r in lp.open_rows(c, LIVE) if r["journal_ref"] == "xxxx0001")
    assert lp._settle(c, row, 60.0, "ratchet_hit", lp.FILL_BASIS, 0.0,
                      datetime(2026, 10, 21, 11, 0, tzinfo=IST))["status"] == "settled"
    assert _approve(world, "pppp0001")["status"] == "approved"
    assert lp.has_open_position(c, LIVE, "pppp0001")


# ============================================ (iii) the opposite direction

def test_the_opposite_direction_on_the_same_underlying_is_approved(world):
    c = world["c"]
    world["mp"].setattr(pm, "CAPITAL_ROTATION_ENABLED", False)
    _propose_pending("bull0001", "bullish")
    _propose_pending("bear0001", "bearish")
    _propose_pending("bnk00001", "bullish", ticker="NIFTY BANK")
    assert _approve(world, "bull0001")["status"] == "approved"
    v = _approve(world, "bear0001")
    assert v["status"] == "approved", v
    assert journal.get_entry("bear0001")["decision"] == "approved"
    assert lp.has_open_position(c, LIVE, "bear0001")
    assert _approve(world, "bnk00001")["status"] == "approved"       # another underlying
    assert _ledger(world) == []


# =================================== (iv) the entry never blocks itself

def test_the_entrys_own_proposal_time_locks_do_not_block_it(world):
    """Every account locked this entry at proposal time — its own locks are
    this trade, not a second position (the normal approval of (i)'s A)."""
    c = world["c"]
    _propose_pending("self0001")
    assert _active_locks(c, "self0001") == (1, sorted([TWO_L, ROT, LIVE]))
    assert _approve(world, "self0001")["status"] == "approved"
    assert _ledger(world) == []


def _live_view():
    return {"lots": 1, "legs": [
        {"side": "BUY", "option_type": "CE", "strike": 24000.0, "avg_fill_price": 100.0},
        {"side": "SELL", "option_type": "CE", "strike": 24200.0, "avg_fill_price": 30.0}]}


def test_the_entrys_own_live_row_and_locks_never_count_against_it(world):
    """Even a LIVE row keyed on the entry's OWN ref (e.g. an approval that
    crashed after the live arm opened, before the journal write) is not a
    conflict: every source skips the entry's ref. Another ref's identical
    row IS one (the control)."""
    c = world["c"]
    pend = _propose_pending("self0002")
    lp.open_position(c, LIVE, pend, _live_view(), now=datetime(2026, 10, 21, 9, 30, tzinfo=IST))
    assert eg.check_approval(pend, today=DAY) == (True, "allowed")
    assert eg.gate_approval(pend, today=DAY) == (True, "allowed", False)
    other = dict(pend, short_id="othr0002")
    lp.open_position(c, LIVE, other, _live_view(), now=datetime(2026, 10, 21, 9, 31, tzinfo=IST))
    allowed, reason = eg.check_approval(pend, today=DAY)
    assert allowed is False and f"`othr0002` {LIVE}" in reason and "self0002" not in reason


def test_own_ref_is_skipped_in_every_source_unit():
    """_firm_conflicts(exclude_ref=): the primary journal, a LIVE row and an
    approved shadow lock — all under the entry's own ref — add nothing."""
    c = brain_map.connect(":memory:")
    pm.ensure_accounts_schema(c)
    lp.ensure_schema(c)
    s = _spread()
    own = {"short_id": "own00001", "date": "2026-10-20", "ticker": "NIFTY 50", "action": "SPREAD",
           "decision": "approved", "outcome": None, "spread": s, "signal": "t", "why": "t"}
    assert pm.paper_request_entry(c, TWO_L, "own00001", 1000.0)["approved"]
    lp.open_position(c, LIVE, own, _live_view(), now=datetime(2026, 10, 21, 9, 30))
    got, unavailable = eg._firm_conflicts("NIFTY 50", "bullish", [own], DAY, c)
    assert [x["trade_id"] for x in got] == ["own00001"] and unavailable is None
    assert got[0]["held_by"] == [PRIMARY, TWO_L, LIVE]
    assert eg._firm_conflicts("NIFTY 50", "bullish", [own], DAY, c,
                              exclude_ref="own00001") == ([], None)


# =========================================== (v) an unreadable firm view

def test_an_unreadable_firm_view_fails_open_and_approves_with_the_note(world, capsys):
    """LIVE holds X on the slot, but brain_map.db cannot be read at approval
    (here: not a database). The gate's contract: judge on the primary
    journal alone, which has nothing, so it approves and prints why."""
    c = world["c"]
    world["mp"].setattr(pm, "CAPITAL_ROTATION_ENABLED", False)
    _propose_pending("pppp0005")
    _propose_pending("xxxx0005")
    assert _approve(world, "xxxx0005")["status"] == "approved"
    _settle_primary_only(c, "xxxx0005")
    junk = world["tmp"] / "corrupt.db"
    junk.write_bytes(b"this is not a sqlite database" * 64)
    world["mp"].setattr(brain_map, "DEFAULT_DB_PATH", junk)
    capsys.readouterr()

    assert _approve(world, "pppp0005")["status"] == "approved"

    out = capsys.readouterr().out
    assert "exposure gate at approval: firm-wide view unavailable" in out
    assert "judged on the primary journal only" in out
    assert _ledger(world) == []


def test_an_unreadable_firm_view_still_blocks_on_the_primary_journal():
    class _Broken:
        def execute(self, *a, **k):
            raise __import__("sqlite3").OperationalError("database is locked")
    pend = {"short_id": "pend0006", "ticker": "NIFTY 50", "decision": "pending_approval",
            "spread": _spread()}
    held = dict(pend, short_id="held0006", decision="approved", outcome=None)
    allowed, reason = eg.check_approval(pend, entries=[held, pend], today=DAY, conn=_Broken())
    assert allowed is False and f"`held0006` {PRIMARY}" in reason
    allowed, reason = eg.check_approval(pend, entries=[pend], today=DAY, conn=_Broken())
    assert allowed is True and "firm-wide view unavailable" in reason and "database is locked" in reason


def test_an_unreadable_journal_fails_the_recheck_open(monkeypatch, capsys):
    monkeypatch.setattr(journal, "read_all", lambda: (_ for _ in ()).throw(ValueError("bad line")))
    pend = {"short_id": "pend0007", "ticker": "NIFTY 50", "spread": _spread()}
    allowed, reason, first = eg.gate_approval(pend, today=DAY)
    assert (allowed, first) == (True, False) and "exposure gate unavailable" in reason
    assert "failing open" in capsys.readouterr().out


def test_an_unclassifiable_entry_is_never_blocked():
    assert eg.check_approval({"short_id": "x", "ticker": "NIFTY 50", "spread": {"legs": []}},
                             entries=[], today=DAY)[0] is True
    # the row's view (regime.trend) classifies a legacy spread without a stamp,
    # exactly as gate_entry falls back to the proposal's view
    assert eg._row_direction({"ticker": "NIFTY 50", "spread": {"strategy": "mystery"},
                              "regime": {"trend": "bearish"}}) == ("NIFTY 50", "bearish")
    assert eg._row_direction({"ticker": "NIFTY 50", "spread": {"strategy": "mystery"},
                              "regime": {"trend": "unknown"}}) == ("NIFTY 50", None)


# ============================================ (vi) a rejection is never blocked

def test_a_rejection_is_never_blocked(world):
    c = world["c"]
    _propose_pending("aaaa0008")
    _propose_pending("bbbb0008")
    assert _approve(world, "aaaa0008")["status"] == "approved"
    v = _approve(world, "bbbb0008", approve=False)
    assert v["status"] == "rejected"
    assert journal.get_entry("bbbb0008")["decision"] == "rejected"
    assert _active_locks(c, "bbbb0008") == (0, [])             # released at zero
    assert _ledger(world) == []


# ================================ the order of the three approval refusals

def test_the_window_refusal_runs_before_the_exposure_recheck(world):
    """An entry that is BOTH inside its forced-exit window and on a taken
    slot is refused by the cheap window check: no exposure ledger line."""
    _propose_pending("aaaa0009")
    _propose_pending("bbbb0009")
    assert _approve(world, "aaaa0009")["status"] == "approved"
    v = _approve(world, "bbbb0009", day=LATE)
    assert v["status"] == op.INSIDE_EXIT_WINDOW
    assert _ledger(world) == []


def test_the_exposure_recheck_runs_before_the_margin_gate(world):
    """A blocked approval never reaches the capital layer: the margin gate,
    the shadow judgement and the venue are not called at all."""
    _propose_pending("aaaa0010")
    _propose_pending("bbbb0010")
    assert _approve(world, "aaaa0010")["status"] == "approved"
    for name in ("gate_headless_entry",):
        world["mp"].setattr(pm, name, lambda *a, **k: pytest.fail("margin gate reached"))
    world["mp"].setattr(op, "_judge_shadow_accounts", lambda *a, **k: pytest.fail("shadows judged"))
    world["mp"].setattr(op, "_execute_paper_entry", lambda *a, **k: pytest.fail("venue reached"))
    assert _approve(world, "bbbb0010")["status"] == "exposure_blocked"


# =========== F-review gaps, for BOTH refusals: D7-expired locks, prefetches

@pytest.mark.parametrize("refusal", ["inside_exit_window", "exposure_blocked"])
def test_a_refused_approval_after_the_d7_sweep_retakes_no_lock(world, refusal):
    """The realistic late tap: the 15:30 sweep already expired every lock
    the pending entry held. A refusal must leave them expired: no margin
    re-request, no shadow lock written, nothing else either."""
    c = world["c"]
    if refusal == "exposure_blocked":
        _propose_pending("hold0011")
        assert _approve(world, "hold0011")["status"] == "approved"
    pend = _propose_pending("late0011")
    assert set(pm.expire_pending_lock(c, "late0011")) == {PRIMARY, TWO_L, ROT, LIVE}
    assert _active_locks(c, "late0011") == (0, [])
    world["prefetch"]["live"].clear(), world["prefetch"]["rotation"].clear()
    world["chain_calls"].clear()
    before = _snapshot(c)

    v = _approve(world, "late0011", day=LATE if refusal == "inside_exit_window" else DAY)

    assert v["status"] == refusal
    assert _active_locks(c, "late0011") == (0, [])
    assert _snapshot(c) == before                    # no paper_margin_locks / margin_locks write
    assert journal.get_entry("late0011") == pend
    # (b) neither network prefetch ran, and nothing re-quoted inside the lock
    assert world["prefetch"] == {"live": [], "rotation": []}
    assert world["chain_calls"] == []


@pytest.mark.parametrize("refusal", ["inside_exit_window", "exposure_blocked"])
def test_a_refused_approval_never_calls_the_rotation_prefetch(world, refusal):
    """With the rotation arm ON, _prefetch_rotation (marks + an eviction
    candidate's chain) is skipped for a refused approval."""
    assert pm.rotation_enabled()
    if refusal == "exposure_blocked":
        _propose_pending("hold0012")
        assert _approve(world, "hold0012")["status"] == "approved"
        # the spies are live: an approval that goes ahead DOES prefetch
        assert world["prefetch"]["rotation"] == ["hold0012"]
        assert world["prefetch"]["live"] == ["hold0012"]
    _propose_pending("late0012")
    world["prefetch"]["live"].clear(), world["prefetch"]["rotation"].clear()
    v = _approve(world, "late0012", day=LATE if refusal == "inside_exit_window" else DAY)
    assert v["status"] == refusal
    assert world["prefetch"]["rotation"] == [] and world["prefetch"]["live"] == []


def test_the_prefetch_precheck_only_skips_network_it_never_decides(world, monkeypatch):
    """If the pre-check (outside the lock) thinks the slot is taken but the
    fresh row inside the lock is clear, the approval goes ahead (as an
    un-prefetched one): only the in-lock verdict decides."""
    c = world["c"]
    world["mp"].setattr(pm, "CAPITAL_ROTATION_ENABLED", False)
    _propose_pending("pre00013")
    monkeypatch.setattr(op, "_prefetch_sees_refusal", lambda ref, today: True)
    v = _approve(world, "pre00013")
    assert v["status"] == "approved"
    assert world["prefetch"]["live"] == []               # skipped by the (wrong) pre-check
    assert lp.has_open_position(c, LIVE, "pre00013")      # the live arm re-quoted inside


def test_the_precheck_is_quiet_and_never_raises(world, monkeypatch, capsys):
    _propose_pending("aaaa0014")
    _propose_pending("bbbb0014")
    assert _approve(world, "aaaa0014")["status"] == "approved"
    capsys.readouterr()
    assert op._prefetch_sees_refusal("bbbb0014", DAY) is True
    assert op._prefetch_sees_refusal("aaaa0014", DAY) is False      # not pending any more
    assert op._prefetch_sees_refusal("nope0014", DAY) is False
    assert _ledger(world) == [] and capsys.readouterr().out == ""
    monkeypatch.setattr(journal, "get_entry", lambda k: 1 / 0)
    assert op._prefetch_sees_refusal("bbbb0014", DAY) is False


# ===================================================== every door

def _held_row(ref="held0099"):
    return {"short_id": ref, "date": "2026-10-19", "action": "SPREAD", "ticker": "NIFTY 50",
            "shares": 65, "price": 70.0, "signal": "t", "decision": "approved", "why": "t",
            "outcome": None, "spread": _spread()}


def _pending_row(ref="api00099"):
    s = _spread()
    s["margin"] = None                   # the door tests never reach the capital layer
    return {"short_id": ref, "date": "2026-10-20", "action": "SPREAD", "ticker": "NIFTY 50",
            "shares": 65, "price": 70.0, "signal": "t", "decision": "pending_approval",
            "why": "(headless proposal)", "outcome": None, "spread": s}


@pytest.fixture
def doors(monkeypatch, tmp_path):
    monkeypatch.setattr(eg, "LEDGER_PATH", tmp_path / "exposure_blocks.jsonl")
    monkeypatch.setattr(op, "_today", lambda: DAY)
    notes = []
    monkeypatch.setattr(op, "_notify_discord", lambda text, *a, **k: notes.append(text))
    monkeypatch.setattr("src.notifier.fire_broadcast",
                        lambda *a, **k: pytest.fail("no broadcast for a refusal"))
    monkeypatch.setattr(op, "_execute_paper_entry", lambda *a, **k: pytest.fail("no ticket"))
    monkeypatch.setattr(op, "_prefetch_live_requote", lambda ref: pytest.fail("no prefetch"))
    monkeypatch.setattr(op, "_prefetch_rotation", lambda ref: pytest.fail("no prefetch"))
    journal.log(_held_row())
    return notes


def test_the_api_bridge_answers_409_and_writes_nothing(doors, monkeypatch):
    from fastapi.testclient import TestClient
    from src.api_server import app
    journal.log(_pending_row())
    before = journal.read_all()
    monkeypatch.setenv("API_KEY", "k")
    r = TestClient(app).post("/api/discord/action", headers={"X-API-Key": "k"},
                             json={"action": "approve", "trade_id": "api00099", "why": "tap"})
    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False and body["status"] == "exposure_blocked"
    assert body["trade_id"] == "api00099"
    assert "`held0099` PAPER_10L" in body["error"] and "decision #68" in body["error"]
    assert journal.read_all() == before
    assert doors == []                                   # a human tap: no card


def test_the_discord_bot_says_why_and_keeps_the_buttons():
    from src import discord_bot as bot
    note, retire = bot._decision_note("api00099", 409, {
        "ok": False, "status": "exposure_blocked",
        "error": "exposure gate: 1 open bullish position(s) on NIFTY 50 already "
                 "(`held0099` PAPER_10L) — max one per underlying+direction, firm-wide, "
                 "re-checked at approval (decision #68)"})
    assert retire is False                               # still pending: Reject works
    assert note.startswith("🧱 Can't approve `api00099` — exposure gate:")
    assert "(decision #68). Not journaled; you can still reject it" in note
    assert "already resolved" not in note and "journaled. " not in note
    # F18's answer is untouched
    note, retire = bot._decision_note("x", 409, {"status": "inside_exit_window", "error": "late"})
    assert retire is False and note.startswith("⏱️ Too late to approve")


def test_the_cli_names_the_block_and_counts_nothing_decided(doors, monkeypatch, capsys):
    journal.log(_pending_row())
    monkeypatch.setattr("builtins.input", mock.Mock(side_effect=["y", "tap"]))
    assert op.review_pending() == 0
    assert "not decided: exposure_blocked (exposure gate: 1 open bullish" in capsys.readouterr().out
    assert journal.get_entry("api00099")["decision"] == "pending_approval"
    assert doors == []


# ================================ (vii) auto-approve: handled, never spammed

def test_auto_approve_reports_the_block_once_and_retries_stay_silent(doors, monkeypatch):
    """The proposal gate passed (a race: the slot was taken in the seconds
    before the auto-approval — simulated by passing gate_entry), and the
    approval re-check blocks. The proposal is reported as declined, the entry
    stays pending, and ONE card says so. Retrying, by auto or by hand, adds
    no second card and no second ledger line that day."""
    canned = {"ticker": "NIFTY 50", "action": "SPREAD", "shares": 65, "price": 70.0, "lots": 1,
              "signal": "t", "view": "bullish", "vix": 13.0,
              "spread": dict(_spread(), margin={"total_margin": 17550.0})}
    monkeypatch.setenv(op.AUTO_APPROVE_ENV_KEY, "1")
    monkeypatch.setattr(op, "build_proposal", lambda *a, **k: {"proposal": canned, "reason": "ok"})
    for name in ("_memory_context_for", "_skeptic_note_for"):
        monkeypatch.setattr(op, name, lambda *a, **k: "")
    monkeypatch.setattr(op, "_format_proposal_alert", lambda p, action_note="": "alert")
    monkeypatch.setattr(op, "_judge_shadow_accounts", lambda *a, **k: {})
    monkeypatch.setattr("src.exposure_gate.gate_entry", lambda *a, **k: (True, "allowed"))
    monkeypatch.setattr(pm, "gate_headless_entry", lambda *a, **k: (True, "locked"))
    from src import human_pulse
    monkeypatch.setattr(human_pulse, "auto_approve_tripped", lambda *a, **k: False)

    res = op.run_headless("NIFTY 50", state={})

    assert res["proposed"] is True and res["auto_approved"] is False
    assert res["reason"].startswith("proposed; auto-approval declined (exposure gate: ")
    assert "`held0099` PAPER_10L" in res["reason"]
    ref = res["entry"]["short_id"]
    assert journal.get_entry(ref)["decision"] == "pending_approval"
    cards = [n for n in doors if "Exposure gate" in n]
    assert len(cards) == 1
    assert f"auto-approval of `{ref}` declined" in cards[0] and "stays PENDING" in cards[0]
    # retries — auto (human=False) and by hand — say nothing new
    assert op.decide_pending(ref, approve=True, human=False)["status"] == "exposure_blocked"
    assert op.decide_pending(ref, approve=True, human=True)["status"] == "exposure_blocked"
    assert len([n for n in doors if "Exposure gate" in n]) == 1
    lines = [json.loads(l) for l in eg.LEDGER_PATH.read_text().splitlines()]
    assert [(l["stage"], l["trade_id"]) for l in lines] == [("approval", ref)]


def test_an_approval_line_does_not_silence_the_days_first_proposal_note(tmp_path, monkeypatch):
    """The two stages keep separate once-per-day memories: an approval-time
    line for NIFTY 50 bullish must not swallow that day's proposal-time
    Discord note for the same slot."""
    monkeypatch.setattr(eg, "LEDGER_PATH", tmp_path / "exposure_blocks.jsonl")
    held = _held_row()
    pend = dict(_pending_row(), spread=_spread())
    assert eg.gate_approval(pend, entries=[held, pend], today=DAY)[2] is True
    sent = []
    proposal = {"ticker": "NIFTY 50", "view": "bullish", "spread": _spread()}
    allowed, _ = eg.gate_entry(proposal, entries=[held], today=DAY,
                               notify_fn=sent.append, record_fn=lambda **k: None)
    assert allowed is False and len(sent) == 1


def test_an_unwritable_ledger_still_blocks_and_never_announces(tmp_path, monkeypatch):
    """The ledger is bookkeeping: a write that fails (here its parent is a
    FILE) changes neither the verdict nor the de-dup — no line means no
    `first_today`, so no card can repeat on every retry."""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    monkeypatch.setattr(eg, "LEDGER_PATH", blocker / "exposure_blocks.jsonl")
    held, pend = _held_row(), _pending_row()
    allowed, reason, first = eg.gate_approval(pend, entries=[held, pend], today=DAY)
    assert (allowed, first) == (False, False) and "`held0099` PAPER_10L" in reason


# ====================== Chunk 2 close-out: a MARGIN block is a refusal too

def _margin_api_row():
    return {"short_id": "mrg00001", "date": "2026-10-06", "action": "SPREAD", "ticker": "NIFTY BANK",
            "shares": 35, "price": 70.0, "signal": "t", "decision": "pending_approval",
            "why": "(headless proposal)", "outcome": None,
            "spread": {"strategy": "iron_condor", "direction": "neutral", "expiry": "2026-10-27",
                       "lot_size": 35, "lots": 1, "legs": [],
                       "margin": {"total_margin": 50_000.0}}}


def test_every_approval_refusal_is_one_named_set():
    assert op.MARGIN_BLOCKED == "margin_blocked"
    assert set(op.APPROVAL_REFUSALS) == {op.INSIDE_EXIT_WINDOW, op.EXPOSURE_BLOCKED,
                                         op.MARGIN_BLOCKED}


def test_the_api_bridge_answers_409_on_a_margin_block_and_writes_nothing(monkeypatch):
    """It used to answer 200 ok:true {"decision": "margin_blocked"}, so the
    Discord bot said "journaled" and retired the buttons of an entry that was
    still pending."""
    from fastapi.testclient import TestClient
    from src.api_server import app
    from src import exposure_gate
    from src import portfolio_manager as pm
    entry = _margin_api_row()
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setattr(op, "_today", lambda: date(2026, 10, 6))
    monkeypatch.setattr(op.journal, "read_all", lambda: [entry])
    rewrite = mock.Mock()
    monkeypatch.setattr(op.journal, "rewrite_all", rewrite)
    monkeypatch.setattr(op, "_prefetch_live_requote", lambda ref: None)
    monkeypatch.setattr(op, "_prefetch_rotation", lambda ref: None)
    monkeypatch.setattr(exposure_gate, "gate_approval", lambda e, **k: (True, "allowed", False))
    monkeypatch.setattr(pm, "gate_headless_entry",
                        lambda ref, margin, conn=None: (False, "margin exhaustion: free Rs.1,000"))
    monkeypatch.setattr(op, "_notify_discord", lambda *a, **k: pytest.fail("no card for a refusal"))
    r = TestClient(app).post("/api/discord/action", headers={"X-API-Key": "k"},
                             json={"action": "approve", "trade_id": "mrg00001", "why": "go"})
    assert r.status_code == 409
    body = r.json()
    assert body == {"ok": False, "status": "margin_blocked",
                    "error": "margin exhaustion: free Rs.1,000", "trade_id": "mrg00001"}
    assert not rewrite.called and entry["decision"] == "pending_approval"


def test_the_discord_bot_explains_a_margin_block_and_keeps_the_buttons():
    from src import discord_bot as bot
    note, retire = bot._decision_note("mrg00001", 409, {"ok": False, "status": "margin_blocked",
                                                        "error": "margin exhaustion: free Rs.1,000"})
    assert retire is False                      # still pending: Reject (or a later Approve) works
    assert "Can't approve `mrg00001`" in note and "margin exhaustion: free Rs.1,000" in note
    assert "journaled. " not in note and "Not journaled" in note
