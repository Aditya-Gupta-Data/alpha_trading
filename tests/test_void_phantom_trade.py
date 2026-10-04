"""
scripts/void_phantom_trade.py — void one phantom options trade at zero
(ledger Issue 41: the 2026-10-02 holiday session filled `7f4a4897`).

Hermetic: ':memory:' sqlite, the conftest's tmp journal, a tmp archive.
"""
import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

from src import brain_map, journal, oms, performance, plan_tracker as pt, portfolio_manager as pm
from src.execution import live_pricer as lp, recon_engine as recon

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("void_phantom_trade", ROOT / "scripts" / "void_phantom_trade.py")
vt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vt)

REF = "ph0001"
TWO_L, ROT, LIVE = pm.ACCOUNT_PAPER_2L, pm.ACCOUNT_PAPER_2L_ROT, pm.ACCOUNT_PAPER_2L_LIVE
NOW = datetime(2026, 10, 4, 19, 0, tzinfo=vt.IST)


def _entry(ref=REF, decision="approved"):
    return {"short_id": ref, "date": "2026-10-02", "ticker": "NIFTY FIN SERVICE", "action": "SPREAD",
            "decision": decision, "outcome": None, "signal": "bearish trend read", "why": "auto",
            "spread": {"strategy": "bear_put_spread", "direction": "bearish", "expiry": "2026-10-27",
                       "lots": 5, "lot_size": 60, "max_loss": 2700.0, "max_profit": 9300.0,
                       "legs": [{"side": "BUY", "option_type": "PE", "strike": 24550.0, "premium": 372.2},
                                {"side": "SELL", "option_type": "PE", "strike": 24350.0, "premium": 327.2}]},
            "accounts": {TWO_L: {"status": "approved", "lots": 1, "margin_rs": 14700.0},
                         ROT: {"status": "approved", "lots": 1, "margin_rs": 14700.0},
                         LIVE: {"status": "rejected", "lots": 0, "margin_rs": None,
                                "reason": "sizing refused: no live bid/ask at proposal"}}}


@pytest.fixture
def world(monkeypatch, tmp_path):
    c = brain_map.connect(":memory:")
    pm.ensure_schema(c)
    pm.ensure_accounts_schema(c)
    oms.ensure_schema(c)
    lp.ensure_schema(c)
    pm.get_account(c)
    monkeypatch.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", True)
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", True)
    journal.log(_entry())
    assert pm.request_entry(c, REF, 73500.0)["approved"]
    for acct in (TWO_L, ROT):
        assert pm.paper_request_entry(c, acct, REF, 14700.0, lots=1, primary_lots=5)["approved"]
    yield c, tmp_path / "voided_trades.jsonl"
    c.close()


def _plan(c, market_open=False):
    return vt.build_plan(c, REF, journal.get_entry(REF), market_open=market_open)


def test_the_plan_names_every_lock_and_changes_nothing(world):
    c, _ = world
    before = pm.account_summary(c)
    plan = _plan(c)
    assert plan["ok"] and not plan["refusals"]
    assert plan["release"] == {"primary": 73500.0, "shadows": {TWO_L: 14700.0, ROT: 14700.0}, "total": 102900.0}
    assert pm.account_summary(c) == before and journal.get_entry(REF)["decision"] == "approved"


def test_void_releases_every_lock_at_zero_and_books_no_money(world):
    c, archive = world
    cash_before = {a: pm.paper_account_summary(c, a)["available_cash"] for a in (pm.ACCOUNT_PAPER_10L, TWO_L, ROT)}
    out = vt.void_trade(c, REF, "NSE closed 2026-10-02", "owner (test)", archive, now=NOW)
    assert out["voided"] and out["locks_still_open"] == 0
    assert out["margin_released_rs"]["total"] == 102900.0
    # every lock released at pnl 0; nothing booked anywhere
    assert c.execute("SELECT released_at IS NOT NULL, pnl_net FROM margin_locks WHERE journal_ref = ?",
                     (REF,)).fetchone()[:] == (1, 0.0)
    rows = c.execute("SELECT account_id, released_at IS NOT NULL, pnl_net FROM paper_margin_locks "
                     "WHERE journal_ref = ? ORDER BY account_id", (REF,)).fetchall()
    assert [tuple(r) for r in rows] == [(TWO_L, 1, 0.0), (ROT, 1, 0.0)]
    s = pm.account_summary(c)
    assert s["realized_pnl"] == 0.0 and s["locked_margin"] == 0.0
    assert s["available_cash"] == cash_before[pm.ACCOUNT_PAPER_10L] + 73500.0
    for acct in (TWO_L, ROT):
        p = pm.paper_account_summary(c, acct)
        assert p["realized_pnl"] == 0.0 and p["available_cash"] == cash_before[acct] + 14700.0
    # the journal row: voided, resolved as a hypothetical with no P&L, the original kept
    e = journal.get_entry(REF)
    assert e["decision"] == "voided"
    o = e["outcome"]
    assert o["resolution"] == "voided" and o["hypothetical"] is True and o["void"] is True
    assert o["pnl_rs"] is None and o["r_multiple"] is None and o["verdict"].startswith("VOID")
    assert e["void"]["by"] == "owner (test)" and e["void"]["before"]["decision"] == "approved"
    assert e["void"]["before"]["accounts"][TWO_L]["status"] == "approved"
    assert e["accounts"][TWO_L]["status"] == "voided" and e["accounts"][LIVE]["status"] == "rejected"
    # the audit trail: one primary event, one per shadow account that held a lock, none for LIVE
    assert c.execute("SELECT COUNT(*) FROM account_events WHERE event_type = 'trade_voided'").fetchone()[0] == 1
    ev = c.execute("SELECT account_id, journal_ref FROM paper_account_events WHERE event_type = 'trade_voided' "
                   "ORDER BY account_id").fetchall()
    assert [tuple(r) for r in ev] == [(TWO_L, REF), (ROT, REF)]
    # the verbatim archive was written BEFORE the change: it holds the approved row and the open locks
    line = json.loads(archive.read_text().strip())
    assert line["journal_row"]["decision"] == "approved" and line["ref"] == REF
    assert line["database"]["primary_locks"][0]["released_at"] is None


def test_a_voided_row_is_invisible_to_the_tracker_the_stats_and_recon(world):
    c, archive = world
    vt.void_trade(c, REF, "NSE closed", "t", archive, now=NOW)
    e = journal.get_entry(REF)
    assert not pt._spread_trackable(e)                                  # the tracker will not resolve it
    assert performance.resolved_returns([e]) == []                      # no stat counts it
    assert REF in recon._settled_refs([e])                              # recon drops its ticket-only rows
    # the orphan-lock reconcile treats it as a hypothetical: zero money
    real = e.get("decision") == "approved" and not e["outcome"].get("hypothetical")
    assert real is False


def test_the_api_scorecard_neither_scores_nor_lists_a_voided_row(world, monkeypatch, tmp_path):
    from src import api, portfolio as pf
    monkeypatch.setattr(pf, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pf, "PORTFOLIO_PATH", tmp_path / "portfolio.json")
    c, archive = world
    vt.void_trade(c, REF, "NSE closed", "t", archive, now=NOW)
    card = api.scorecard()
    assert card["summary"]["scored"] == 0 and card["summary"]["flat"] == 0
    assert card["executed_trades"] == [] and card["skipped_trades"] == [] and card["archetype_stats"] == []


def test_a_second_run_does_nothing(world):
    c, archive = world
    assert vt.void_trade(c, REF, "NSE closed", "t", archive, now=NOW)["voided"]
    again = vt.void_trade(c, REF, "NSE closed", "t", archive, now=NOW)
    assert not again["voided"] and again["reason"] == "already voided"
    assert len(archive.read_text().strip().splitlines()) == 1
    assert c.execute("SELECT COUNT(*) FROM account_events WHERE event_type = 'trade_voided'").fetchone()[0] == 1
    assert _plan(c)["already_voided"]


def test_refuses_when_the_market_is_open_or_the_row_is_not_an_open_approved_spread(world):
    c, archive = world
    assert "market is open" in _plan(c, market_open=True)["refusals"][0]
    assert vt.build_plan(c, "nope", None, False)["refusals"] == ["no journal row with short_id nope"]
    journal.update_entry(REF, lambda e: e.update(decision="pending_approval"))
    assert any("not 'approved'" in r for r in _plan(c)["refusals"])
    out = vt.void_trade(c, REF, "x", "t", archive, now=NOW)
    assert not out["voided"] and "not 'approved'" in out["reason"]
    journal.update_entry(REF, lambda e: e.update(decision="approved", outcome={"resolution": "ratchet_hit"}))
    assert any("already resolved" in r for r in _plan(c)["refusals"])
    assert not archive.exists()                                         # a refusal archives nothing
    assert c.execute("SELECT COUNT(*) FROM margin_locks WHERE released_at IS NULL").fetchone()[0] == 1


def test_refuses_a_trade_with_an_exit_ticket_or_an_open_live_position(world):
    c, archive = world
    c.execute("INSERT INTO trade_tickets (ticket_id, journal_ref, underlying, strategy, direction, lots, lot_size, "
              "source, status, issued_at, updated_at, note, account_id, kind) VALUES "
              "('tkt:x', ?, 'NIFTY FIN SERVICE', 'bear_put_spread', 'bearish', 5, 60, 't', 'FILLED', "
              "'2026-10-03T05:35:00+05:30', '2026-10-03T05:35:00+05:30', 'EXIT ratchet_hit', 'PAPER_10L', 'EXIT')",
              (REF,))
    c.commit()
    assert any("EXIT ticket" in r for r in _plan(c)["refusals"])
    c.execute("DELETE FROM trade_tickets")
    c.execute("INSERT INTO paper_live_positions (account_id, journal_ref, ticker, expiry, lots, lot_size, legs_json, "
              "entry_mark_ps, width_ps, max_profit_ps, max_loss_ps, opened_at, state) VALUES "
              "(?, ?, 'NIFTY FIN SERVICE', '2026-10-27', 1, 60, '[]', 45.0, 200.0, 155.0, 45.0, '2026-10-02T09:20:00', 'open')",
              (LIVE, REF))
    c.commit()
    out = vt.void_trade(c, REF, "x", "t", archive, now=NOW)
    assert not out["voided"] and "live-quote account holds an open live position" in out["reason"]
    assert journal.get_entry(REF)["decision"] == "approved"


def test_the_cli_refuses_without_a_reason_and_dry_runs_by_default(world, monkeypatch, capsys):
    c, _ = world
    assert vt.main(["--ref", REF]) == 1
    assert "Refusing to void without --why" in capsys.readouterr().out
