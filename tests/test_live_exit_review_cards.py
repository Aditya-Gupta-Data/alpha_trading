"""Decision #133 (Architect ruling 2026-10-09): a half-filled or stuck
PAPER_2L_LIVE exit pages a human — ONE `live_exit_needs_review` Discord card
per position per event type per IST day, sent at once (past the daily
budget), never only a ledger row."""
import pytest

from src import notifier, oms
from src.execution import live_pricer as lp
from tests.test_live_exit_ownership_and_fills import (  # noqa: F401
    world, _open, _row, _tick, _fail_second_leg_fill, _broken_cancel, ENTRY, LATER, OPEN, PX, LIVE)


@pytest.fixture
def cards(monkeypatch):
    sent = []
    monkeypatch.setattr(notifier, "fire_broadcast", lambda payload: sent.append(payload))
    return sent


def _events(c, kind, ref="lx0001"):
    return c.execute("SELECT COUNT(*) FROM paper_account_events WHERE account_id = ? AND journal_ref = ? "
                     "AND event_type = ?", (LIVE, ref, kind)).fetchone()[0]


def test_a_half_filled_exit_pages_once_a_day(world, monkeypatch, cards):
    c = world
    ref = _open(c)
    assert _tick(c, 0, ENTRY, 1000)["marked"] == 1
    real_fill = _fail_second_leg_fill(monkeypatch)
    assert lp._exit(c, _row(c), PX, "profit_take", OPEN.replace(minute=1))["status"] == "exit_error"
    monkeypatch.setattr(oms, "apply_fill", real_fill)
    _tick(c, 2, LATER, 1060)                       # the resume names it partial
    _tick(c, 3, LATER, 1120)                       # still partial: no second card
    review = [p for p in cards if p["event"] == lp.EXIT_REVIEW_CARD]
    assert len(review) == 1 and review[0]["short_id"] == ref
    assert "basket partly filled" in review[0]["description"] and "NIFTY 50" in review[0]["description"]
    assert _events(c, lp.EVENT_EXIT_PARTIAL) == 1
    # still half-filled 14 minutes later (completion held): its own card was
    # sent — it is never paged a second time as 'stuck'
    monkeypatch.setattr(lp, "_complete_exit",
                        lambda *a, **k: {"status": "partial_held", "journal_ref": ref, "reason": "held"})
    _tick(c, 15, LATER, 1900)
    assert len([p for p in cards if p["event"] == lp.EXIT_REVIEW_CARD]) == 1
    assert _events(c, lp.EVENT_EXIT_STUCK) == 0 and _row(c)["state"] == "exiting"


def test_a_stuck_exit_pages_after_two_quote_intervals_and_only_once(world, monkeypatch, cards):
    c = world
    ref = _open(c)
    monkeypatch.setattr(oms, "cancel_ticket", _broken_cancel)       # nothing can resolve the attempt

    class _NoFill:
        @staticmethod
        def sweep(conn, **kw):
            return {}
    res = lp._exit(c, _row(c), PX, "ratchet_hit", OPEN, venue_mod=_NoFill)
    assert res["status"] == "exit_error" and _row(c)["state"] == "exiting"
    for minute in (1, 5, 9):                                        # under 2 x 300 s: not stuck yet
        _tick(c, minute, ENTRY, 1000 + minute * 60)
    assert [p for p in cards if p["event"] == lp.EXIT_REVIEW_CARD] == []
    _tick(c, 10, ENTRY, 1600)                                       # 600 s: stuck
    _tick(c, 11, ENTRY, 1660)
    review = [p for p in cards if p["event"] == lp.EXIT_REVIEW_CARD]
    assert len(review) == 1 and review[0]["short_id"] == ref
    assert "has been 'exiting' since" in review[0]["description"] and "needs a human look" in review[0]["description"]
    assert _events(c, lp.EVENT_EXIT_STUCK) == 1


def test_an_exit_that_completes_normally_never_pages(world, cards):
    c = world
    _open(c)
    assert lp._exit(c, _row(c), PX, "ratchet_hit", OPEN)["status"] == "settled"
    _tick(c, 15, ENTRY, 1900)
    assert [p for p in cards if p["event"] == lp.EXIT_REVIEW_CARD] == []


def test_the_review_card_is_sent_past_the_daily_budget(tmp_path):
    state = tmp_path / "budget.json"
    state.write_text('{"date": "%s", "sent": 99}' % notifier._ist_today_str())
    out = notifier.budget_gate({"event": lp.EXIT_REVIEW_CARD}, state_path=state,
                               queue_path=tmp_path / "q.jsonl", enabled=True)
    assert out == "send"
    assert notifier.budget_gate({"event": "live_entry_needs_review"}, state_path=state,
                                queue_path=tmp_path / "q.jsonl", enabled=True) == "send"   # Chunk 4 R3
