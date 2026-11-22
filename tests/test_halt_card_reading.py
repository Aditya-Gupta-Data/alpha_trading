"""Issue 59: the SYSTEM PAUSED card must quote why the latch tripped, not only today's stale-price reading. Hermetic ':memory:'."""
import sqlite3
from datetime import datetime

import pytest

from src import equity_history
from src import portfolio_manager as pm

LATCHED_AT = datetime(2026, 11, 9, 9, 18, 0)          # two fresh readings at 12.27% latch the halt
NEXT_MORNING = datetime(2026, 11, 10, 8, 5, 0)        # pre-market: readings are >45 min old


@pytest.fixture
def conn(monkeypatch):
    c = sqlite3.connect(":memory:")
    pm.ensure_schema(c)
    pm.ensure_accounts_schema(c)
    pm.get_account(c)
    equity_history.ensure_schema(c)
    monkeypatch.setattr(pm, "_now_ist_naive", lambda: LATCHED_AT)
    yield c
    c.close()


@pytest.fixture
def sent(monkeypatch):
    cards = []
    monkeypatch.setattr("src.notifier.fire_broadcast", lambda p: cards.append(p))
    return cards


def _read(c, minute, net, marked="15/15"):
    c.execute("INSERT INTO net_equity_history (account_id, ts, equity, unrealized_pnl, net_equity, "
              "starting_capital, marks_as_of, marked_of) VALUES ('PAPER_10L', ?, 0, 0, ?, 0, NULL, ?)",
              (LATCHED_AT.replace(minute=minute).isoformat(timespec="seconds"), net, marked))
    c.commit()


def _latch_on_true_equity(c):
    """Realized drawdown stays 0%; two fresh fully priced readings at ~12.3% latch the halt."""
    _read(c, 10, 877_300)
    _read(c, 15, 877_300)
    assert pm.trading_halted(c) is True and pm.halt_latched(c) is True


def test_the_card_quotes_the_latch_reason_when_the_reading_has_gone_stale(conn, sent, monkeypatch):
    _latch_on_true_equity(conn)
    conn.execute("DELETE FROM account_events WHERE event_type = 'ruin_halt_card'")     # drop the latch-moment card
    conn.commit()
    sent.clear()
    monkeypatch.setattr(pm, "_now_ist_naive", lambda: NEXT_MORNING)                   # readings now stale
    assert pm.true_equity_drawdown_pct(conn) is None
    assert pm.ruin_drawdown_pct(conn) == 0.0                                          # the figure the old card showed
    pm._ruin_halt_card(conn)
    text = sent[0]["description"]
    assert "booked results only" in text
    assert "true-equity drawdown 12.27% >= 10% of peak equity" in text                # why the latch actually tripped
    assert "the halt latched " in text and " IST on: " in text


def test_the_card_says_open_losses_are_counted_when_fresh_readings_exist(conn, sent):
    _latch_on_true_equity(conn)
    conn.execute("DELETE FROM account_events WHERE event_type = 'ruin_halt_card'")
    conn.commit()
    sent.clear()
    pm._ruin_halt_card(conn)
    text = sent[0]["description"]
    assert "12.27% from peak equity now (open losses counted)" in text
    assert "limit 10%" in text


def test_the_digest_banner_carries_the_same_reading(conn, sent, monkeypatch):
    _latch_on_true_equity(conn)
    monkeypatch.setattr(pm, "_now_ist_naive", lambda: NEXT_MORNING)
    banner = pm.halt_banner_lines(conn=conn)
    assert banner and "SYSTEM PAUSED" in banner[0]
    assert "booked results only" in banner[0]
    assert "true-equity drawdown 12.27%" in banner[0]
    assert "exits" in banner[0].lower()                                               # still says exits continue


def test_a_realized_latch_is_quoted_as_realized(conn, sent):
    pm.request_entry(conn, "wipeout", 200_000.0)
    pm.release_margin(conn, "wipeout", -150_000.0)                                    # 15% realized drawdown
    conn.execute("UPDATE margin_locks SET released_at = '2000-01-01T10:00:00' WHERE journal_ref = 'wipeout'")
    conn.commit()
    assert pm.trading_halted(conn)
    reading = pm.ruin_halt_reading(conn)
    assert "15.00% from peak equity now (booked results only" in reading
    assert "realized drawdown 15.00%" in reading


def test_the_reading_is_still_a_sentence_when_no_latch_row_exists(conn):
    assert pm._latch_record(conn) is None
    reading = pm.ruin_halt_reading(conn)
    assert reading.startswith("0.00% from peak equity now") and "latched" not in reading


def test_the_audit_row_records_the_reading_and_the_card_stays_once_per_day(conn, sent):
    _latch_on_true_equity(conn)
    pm._ruin_halt_card(conn)
    pm._ruin_halt_card(conn)
    rows = [r[0] for r in conn.execute("SELECT detail FROM account_events WHERE event_type = 'ruin_halt_card'")]
    assert len(rows) == 1 and "SYSTEM PAUSED card fired (drawdown" in rows[0] and "latched" in rows[0]
    assert len(sent) <= 1


def test_a_broken_reading_never_breaks_the_card_or_the_banner(conn, sent, monkeypatch):
    _latch_on_true_equity(conn)
    monkeypatch.setattr(pm, "ruin_halt_reading", lambda c: (_ for _ in ()).throw(RuntimeError("boom")))
    pm._ruin_halt_card(conn)                                                          # swallowed, prints a note
    assert pm.halt_banner_lines(conn=conn) == []                                      # fail-safe: [] not a raise


@pytest.fixture(autouse=True)
def _enforced_ruin_latch(monkeypatch):
    """These tests pin the enforced 10% latch; production runs it record-only (#187)."""
    from src import portfolio_manager as _pm
    monkeypatch.setattr(_pm, "RUIN_HALT_BLOCKS_ENTRIES", True)
