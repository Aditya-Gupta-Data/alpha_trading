"""The Shadow Learner (decision #140, paper only): switch wiring, cadence/cap, Court rows,
the firm-holdings exemption, the advisory block recorded instead of a refusal."""
import sqlite3
from src import shadow_learner as sl, portfolio_manager as pm


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    pm.ensure_accounts_schema(c)
    return c


def test_switch_and_account_wiring(monkeypatch):
    assert sl.ACCOUNT in pm.LIVE_ACCOUNTS and sl.ACCOUNT in pm.PAPER_ACCOUNTS
    assert pm.PAPER_ACCOUNTS[sl.ACCOUNT] == 200000.0
    monkeypatch.setattr("src.config.SHADOW_LEARNER_ENABLED", False)
    monkeypatch.setattr(pm, "SHADOW_LEARNER_ENABLED", False)
    assert not sl.enabled() and sl.ACCOUNT not in pm.shadow_account_ids()
    monkeypatch.setattr("src.config.SHADOW_LEARNER_ENABLED", True)
    monkeypatch.setattr(pm, "SHADOW_LEARNER_ENABLED", True)
    monkeypatch.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", True)
    assert sl.enabled() and sl.ACCOUNT in pm.shadow_account_ids()


def test_ready_honours_its_own_cadence_and_cap(monkeypatch):
    c = _conn()
    sl._LAST_TAKE.clear()
    monkeypatch.setattr("src.config.SHADOW_LEARNER_COOLDOWN_SECONDS", 900)
    monkeypatch.setattr("src.config.SHADOW_LEARNER_MAX_OPEN", 1)
    assert sl.ready("NIFTY 50", now=1000.0, conn=c)[0]
    sl.arm("NIFTY 50", now=1000.0)
    ok, why = sl.ready("NIFTY 50", now=1300.0, conn=c)
    assert not ok and "cadence" in why
    assert sl.ready("NIFTY BANK", now=1300.0, conn=c)[0]            # per underlying
    assert sl.ready("NIFTY 50", now=1901.0, conn=c)[0]              # 15 min later
    from src.execution import live_pricer as lp
    lp.ensure_schema(c)
    c.execute("INSERT INTO paper_live_positions (account_id, journal_ref, ticker, strategy, direction, expiry, "
              "lots, lot_size, entry_mark_ps, max_loss_ps, max_profit_ps, width_ps, state, opened_at, legs_json) "
              "VALUES (?, 'x1', 'NIFTY 50', 'bear_put_spread', 'bearish', '2026-10-27', 1, 65, 1, 1, 1, 2, 'open', 'now', '[]')",
              (sl.ACCOUNT,))
    ok, why = sl.ready("NIFTY BANK", now=1300.0, conn=c)
    assert not ok and "cap" in why


def test_court_rows_record_and_resolve_with_the_real_r():
    c = _conn()
    entry = {"short_id": "ab12cd34", "ticker": "NIFTY 50", "view": "bearish",
             "spread": {"strategy": "bear_put_spread", "direction": "bearish"}}
    r1 = sl.record_fire(c, entry, fire_date="2026-10-12")
    assert r1["created"] and not sl.record_fire(c, entry, fire_date="2026-10-12")["created"]
    row = {"journal_ref": "ab12cd34", "lots": 2, "lot_size": 65, "max_loss_ps": 10.0}
    assert sl.resolve_from_settle(c, row, pnl_net=650.0, closed_on="2026-10-14T15:20:00")
    assert sl.evidence(c, "bear_put_spread") == {"n": 1, "wins": 1, "r_sum": 0.5}
    from src.validation import trial
    assert trial.shadow_evidence(c, "learner:bear_put_spread") == {"n": 1, "wins": 1}


def test_exposure_gate_never_counts_the_learners_holdings():
    from src import exposure_gate as eg
    from src.execution import live_pricer as lp
    c = _conn(); lp.ensure_schema(c)
    for acct in (sl.ACCOUNT, pm.ACCOUNT_PAPER_2L_LIVE):
        c.execute("INSERT INTO paper_live_positions (account_id, journal_ref, ticker, strategy, direction, expiry, "
                  "lots, lot_size, entry_mark_ps, max_loss_ps, max_profit_ps, width_ps, state, opened_at, legs_json) "
                  "VALUES (?, ?, 'NIFTY 50', 'bear_put_spread', 'bearish', '2026-10-27', 1, 65, 1, 1, 1, 2, 'open', 'now', '[]')",
                  (acct, "ref_" + acct))
    c.execute("INSERT INTO paper_margin_locks (account_id, journal_ref, margin_rs, lots, primary_lots, locked_at) "
              "VALUES (?, 'lockref', 1000, 1, 1, 'now')", (sl.ACCOUNT,))
    held, err = eg._firm_holdings(conn=c)
    assert err is None and {h["account_id"] for h in held} == {pm.ACCOUNT_PAPER_2L_LIVE}


def test_build_proposal_records_the_veto_for_the_learner_instead_of_refusing(monkeypatch):
    from src import options_proposer as op
    from tests.test_options_proposer import make_analysis, make_chain, add_bid_ask
    adv = {"block_bullish": True, "bullish_reason": "distribution"}
    kw = dict(analysis=make_analysis(uptrend=True, rsi=25), vix=15.0, expiry="2099-12-31",
              chain=add_bid_ask(make_chain()), advisory=adv, prices={})
    monkeypatch.setattr(op, "_learner_enabled", lambda: True)
    r = op.build_proposal("NIFTY 50", **kw)                      # real book: the veto is recorded, the structure built
    assert r.get("advisory_block") == "distribution" and r["proposal"] is not None
    r = op.build_proposal("NIFTY 50", book={"cash": 1e6, "positions": []}, **kw)   # sandbox book: refused as before
    assert r["proposal"] is None
    monkeypatch.setattr(op, "_learner_enabled", lambda: False)
    assert op.build_proposal("NIFTY 50", **kw)["proposal"] is None


def test_learner_is_never_margin_blocked_or_halted():
    """#140 (owner): an infinite paper budget — the margin wall and the halts never refuse the learner."""
    c = _conn()
    pm.get_paper_account(c, sl.ACCOUNT)
    spread = {"max_loss": 5000.0, "margin": {"total_margin": 10_000_000.0}}
    sized = pm.size_for_account(c, sl.ACCOUNT, spread, risk_pct=2.0, vix=30.0)
    assert sized["lots"] >= 1 and sized["by_margin"] is None               # no margin wall on lots
    v = pm.paper_request_entry(c, sl.ACCOUNT, "big00001", 10_000_000.0, lots=1)
    assert v["approved"]                                                   # Rs.1 crore locked against Rs.2L
    v2 = pm.paper_request_entry(c, sl.ACCOUNT, "big00002", 10_000_000.0, lots=1)
    assert v2["approved"] and pm.paper_available_cash(c, sl.ACCOUNT) < 0
    r = pm.paper_request_entry(c, pm.ACCOUNT_PAPER_2L, "big00003", 10_000_000.0, lots=1)
    assert not r["approved"] and "margin exhaustion" in r["reason"]        # every other account: the wall holds


def test_take_refuses_cleanly_with_the_venue_off(monkeypatch):
    monkeypatch.setattr("src.config.PAPER_VENUE_ENABLED", False)
    entry = {"short_id": "zz00zz00", "ticker": "NIFTY 50", "spread": {"strategy": "bear_put_spread", "legs": []}}
    v = sl.take(entry, "advisory: test")
    assert v["status"] == "rejected" and "venue off" in v["reason"]
    assert entry["accounts"][sl.ACCOUNT] is v and v["learner_only"] == "advisory: test"
