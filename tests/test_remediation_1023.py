"""Post-audit remediation (Architect 2026-10-23): no learner cap, the #151 gate leads the
primary's cash/sizing refusals, designed refusals are not ops problems, and a live book
overrules an illiquid strike's stale last trade (fallback marks never move the ratchet). Offline."""
import sqlite3
from datetime import date, datetime

from src import options_proposer as op, ops_monitor as om, proposal_ledger as pl, shadow_learner as sl
from src.execution import live_pricer as lp
from tests.test_chunk2_l4_lows import KS_ROW, _chain
from tests.test_event_gates import _build


# --------------------------------------------------------------- learner cap

def test_the_learner_has_no_open_position_cap_by_default(monkeypatch):
    monkeypatch.setattr("src.config.SHADOW_LEARNER_MAX_OPEN", None)
    assert sl.ready("INFY.NS", now=1000.0, conn=object()) == (True, "ready")   # no strategy, no cap: no read


# --------------------------------------------------------------- gate precedence

def test_the_gate_outranks_the_primarys_cash_and_routes_to_the_learner(monkeypatch):
    from src import portfolio as pf
    from tests.test_options_proposer import BIG_BOOK
    monkeypatch.setattr(op, "_gate_days_to_results", lambda u, t=None: (2, None))
    monkeypatch.setattr(op, "_learner_enabled", lambda: True)
    monkeypatch.setattr(pf, "load", lambda *a, **k: dict(BIG_BOOK))
    r = _build(account_equity=200_000.0, account_cash=1_000.0)          # the primary cannot afford one lot
    assert r["proposal"] is not None and r["gate_routed"] is True
    assert r["advisory_block"].startswith("earnings proximity gate: results in 2 day(s)")
    assert r["proposal"]["lots"] == 0 and r["proposal"]["shares"] == 0     # never the primary's to take
    monkeypatch.setattr(op, "_learner_enabled", lambda: False)
    off = _build(account_equity=200_000.0, account_cash=1_000.0)
    assert off["proposal"] is None and off["reason"].startswith("earnings proximity gate")


def test_without_the_gate_the_cash_refusal_is_unchanged(monkeypatch):
    from src import portfolio as pf
    from tests.test_options_proposer import BIG_BOOK
    monkeypatch.setattr(op, "_gate_days_to_results", lambda u, t=None: (9, None))
    monkeypatch.setattr(op, "_learner_enabled", lambda: True)
    monkeypatch.setattr(pf, "load", lambda *a, **k: dict(BIG_BOOK))
    r = _build(account_equity=200_000.0, account_cash=1_000.0)
    assert r["proposal"] is None and r["reason"].startswith("sizing refused")


def test_the_ledger_files_a_gated_refusal_under_the_gate_first():
    reason = ("earnings proximity gate: results in 1 day(s) (<= 5) — iron_butterfly refused (#151); "
              "also REJECTED_POOR_RR: R:R below 1.5 threshold")
    assert pl.classify({"proposed": False, "reason": reason}) == "REJECTED_EVENT_GATE"
    assert pl.classify({"proposed": False, "reason": "advisory: " + reason}) == "REJECTED_EVENT_GATE"
    assert pl.classify({"proposed": False, "reason": "REJECTED_POOR_RR: R:R below 1.5"}) == "REJECTED_POOR_RR"


# --------------------------------------------------------------- ops noise

GATE = ("[Market Loop] INFY.NS: no proposal (advisory: earnings proximity gate: results in 1 day(s) "
        "(<= 5) — iron_butterfly refused (#151)).")
SIZING = "[Market Loop] NIFTY 50: no proposal (sizing refused: 1-lot max loss Rs.32,295 exceeds the 2.5% hard cap)."
CHAIN_DOWN = "[Market Loop] ICICIBANK.NS: no proposal (option chain unavailable)"


def test_designed_refusals_are_not_problems_but_data_failures_still_are():
    assert not om.is_problem_line(GATE) and om.is_designed_block(GATE)
    assert not om.is_problem_line(SIZING) and om.is_designed_block(SIZING)
    assert om.is_problem_line(CHAIN_DOWN) and not om.is_designed_block(CHAIN_DOWN)
    assert om.is_problem_line(SIZING + " DH-905")                       # a Dhan code always counts


def test_the_sweep_counts_designed_refusals_apart_and_the_card_says_so(tmp_path):
    (tmp_path / "master_scheduler.log").write_text("\n".join([GATE, GATE, SIZING, CHAIN_DOWN]) + "\n")
    blocks = {}
    problems, _ = om.sweep_logs(tmp_path, {}, blocks=blocks)
    assert [p["line"] for p in problems] == [CHAIN_DOWN]
    assert blocks == {"master_scheduler.log": 3}
    card = om.build_card(problems, [], "2026-10-23 20:30", designed_blocks=3)
    assert "1 problem line(s)" in card and "3 designed refusal(s)" in card


# --------------------------------------------------------------- frozen marks

SHORT = {"side": "SELL", "option_type": "PE", "strike": 14250.0}
LONG = {"side": "BUY", "option_type": "PE", "strike": 14500.0}


def test_a_live_book_overrules_a_stale_last_trade_only_when_it_vouches():
    q = {"bid": 900.0, "ask": 920.0, "ltp": 520.9}                       # the 10-22 14250PE shape
    assert lp.crossed_close_price(SHORT, q, trust_book=False) == (None, "quote 920 is >50% off last 520.9")
    assert lp.crossed_close_price(SHORT, q, trust_book=True) == (920.0, None)
    wide = {"bid": 100.0, "ask": 920.0, "ltp": 520.9}                    # last trade inside a wide book
    assert lp.crossed_close_price(SHORT, wide, trust_book=True)[0] is None
    torn = {"bid": 200.0, "ask": 920.0, "ltp": 100.0}                    # ask > 3 x bid: the book cannot vouch
    assert lp.crossed_close_price(SHORT, torn, trust_book=True)[0] is None
    assert lp.crossed_close_price(LONG, {"bid": 900.0, "ask": 920.0, "ltp": 520.9}, trust_book=True) == (900.0, None)


def test_the_book_is_never_trusted_in_the_opening_quarter_hour_or_under_pytest(monkeypatch):
    assert lp.book_trust_open(datetime(2026, 10, 26, 11, 0)) is False    # pytest: always off
    monkeypatch.setattr(lp, "_is_test_env", lambda: False)
    assert lp.book_trust_open(datetime(2026, 10, 26, 9, 20)) is False
    assert lp.book_trust_open(datetime(2026, 10, 26, 9, 30)) is True


def test_evaluate_marks_on_the_vouching_book_once_trusted(monkeypatch):
    chain = _chain({(24000, "CE"): (230.0, 232.0, 231.0), (24200, "CE"): (150.0, 155.0, 74.0)})
    today = date(2026, 10, 8)
    ev = lp.evaluate(dict(KS_ROW), chain, today, now=datetime(2026, 10, 8, 11, 0))
    assert not ev["ok"] and "155 is >50% off last 74" in ev["reason"]
    monkeypatch.setattr(lp, "book_trust_open", lambda now=None: True)
    ev = lp.evaluate(dict(KS_ROW), chain, today, now=datetime(2026, 10, 8, 11, 0))
    assert ev["ok"] and ev["mark_ps"] == 75.0                             # 230 bid - 155 ask, the real exit


def test_an_abstaining_read_carries_a_fallback_mark_on_the_last_trade():
    chain = _chain({(24000, "CE"): (230.0, 232.0, 231.0), (24200, "CE"): (73.0, None, 74.0)})   # no ask on the short
    ev = lp.evaluate(dict(KS_ROW), chain, date(2026, 10, 8))
    assert not ev["ok"] and "no ask to buy the short leg back" in ev["reason"]
    assert ev["fallback"] == {"mark_ps": 156.0, "profit_ps": 86.0, "capture_pct": 66.15}
    assert lp.fallback_mark(dict(KS_ROW), _chain({(24000, "CE"): (230.0, 232.0, 231.0)})) is None   # leg absent


def test_a_fallback_mark_moves_the_mark_and_its_time_never_the_ratchet():
    conn = sqlite3.connect(":memory:")
    lp.ensure_schema(conn)
    conn.execute("INSERT INTO paper_live_positions (account_id, journal_ref, ticker, strategy, direction, expiry, "
                 "lots, lot_size, legs_json, entry_mark_ps, width_ps, max_profit_ps, max_loss_ps, opened_at, "
                 "state, ratchet_peak_pct, ratchet_lock_pct, quote_ts, last_mark_ts) "
                 "VALUES ('PAPER_2L_LIVE', 'r1', 'NIFTY 50', 'bull_call_spread', 'bullish', '2026-10-29', 1, 65, "
                 "'[]', 70, 200, 130, 70, '2026-10-06T10:00:00', 'open', 45.0, 30.0, '2026-10-21T15:25:15', "
                 "'2026-10-21T15:25:15')")
    lp._record_fallback_mark(conn, {"account_id": "PAPER_2L_LIVE", "journal_ref": "r1"},
                             {"mark_ps": 156.0, "profit_ps": 86.0, "capture_pct": 66.15}, "2026-10-26T10:00:00")
    row = conn.execute("SELECT last_mark_ts, last_mark_ps, last_capture_pct, mark_basis, quote_ts, "
                       "ratchet_peak_pct, ratchet_lock_pct FROM paper_live_positions").fetchone()
    assert row == ("2026-10-26T10:00:00", 156.0, 66.15, "fallback:ltp", "2026-10-21T15:25:15", 45.0, 30.0)


# --------------------------------------------------------------- event engine quota + dedup

def test_a_repeated_headline_never_reaches_the_llm_or_a_second_card(tmp_path, monkeypatch):
    from src.event_engine import detect, emit
    from tests.test_event_engine import CFG, CLASSIFIED, EXPOSED, FLAT_BARS, POLICY_TEXT, FakeExtractor
    from src.event_engine.llm import EventLLM
    monkeypatch.setattr(emit, "STATE_DIR", tmp_path / "state")
    fx = FakeExtractor(classify=CLASSIFIED, expose=EXPOSED)
    llm = EventLLM(extractor=fx, cache_dir=tmp_path / "c", ledger=tmp_path / "l.jsonl", cap=100, run_cap=50)
    items = [{"title": "Indian banks open cheque books for &amp; M&A", "text": POLICY_TEXT, "url": "https://a"},
             {"title": "Indian banks open cheque books for & M&A!", "text": POLICY_TEXT + " ", "url": "https://b"}]
    assert detect.title_key(items[0]) == detect.title_key(items[1])
    s = emit.run(items=items, llm=llm, bars_fn=lambda sym: FLAT_BARS, card_dir=tmp_path / "cards", do_ship=False,
                 seen=detect.SeenLedger(tmp_path / "seen.txt"), cfg=dict(CFG))
    assert len(s["cards"]) == 1 and s["skipped"] == {"duplicate headline": 1} and fx.calls == 2


def test_one_run_spends_at_most_its_share_and_the_next_run_still_has_budget(tmp_path):
    from src.event_engine.detect import CLASSIFY_PROMPT as P
    from src.event_engine.llm import EventLLM
    from tests.test_event_engine import FakeExtractor
    ledger = tmp_path / "l.jsonl"
    morning = EventLLM(extractor=FakeExtractor(classify={"x": 1}), cache_dir=tmp_path / "c", ledger=ledger,
                       cap=4, run_cap=2)
    assert [morning.ask(P, str(i)) is not None for i in range(3)] == [True, True, False]
    assert morning.last_skip.startswith("LLM run cap 2 spent")
    evening = EventLLM(extractor=FakeExtractor(classify={"x": 1}), cache_dir=tmp_path / "c", ledger=ledger,
                       cap=4, run_cap=2)
    assert [evening.ask(P, str(i)) is not None for i in range(10, 13)] == [True, True, False]
    late = EventLLM(extractor=FakeExtractor(classify={"x": 1}), cache_dir=tmp_path / "c", ledger=ledger,
                    cap=4, run_cap=2)
    assert late.ask(P, "99") is None and late.last_skip == "LLM daily cap 4 spent"


def test_the_shipped_config_carries_the_100_call_day_split_50_50():
    import json
    from pathlib import Path
    ee = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text())["event_engine"]
    assert ee["llm_daily_call_cap"] == 300 and ee["llm_run_call_cap"] == 150


# --------------------------------------------------------------- daily participant OI + GEX

def test_catch_up_fetches_only_the_missing_sessions(tmp_path):
    from src.ingestion import participant_oi as poi
    from tests.test_gex_seam2 import CSV
    asked = []

    def fetch(day):
        asked.append(day)
        return (200, CSV.encode()) if day != "2026-10-23" else (404, None)   # today's file not published yet

    poi.run("2026-10-21", root=tmp_path, fetch_fn=lambda d: (200, CSV.encode()))
    out = poi.catch_up(date(2026, 10, 23), sessions=3, root=tmp_path, fetch_fn=fetch, pause=0)
    assert asked == ["2026-10-22", "2026-10-23"]                    # 10-21 already in the lake
    assert [r["written"] for r in out] == [True, False]
    asked.clear()
    poi.catch_up(date(2026, 10, 26), sessions=3, root=tmp_path, fetch_fn=fetch, pause=0)
    assert asked == ["2026-10-23", "2026-10-26"]                     # the late file is retried on Monday


def test_the_vm_schedule_carries_the_daily_gex_job_before_the_sweep():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "scripts" / "setup_cron.sh").read_text()
    line = next(ln for ln in text.splitlines() if "participant_oi --today" in ln)
    assert line.startswith("45 18 * * 1-5 ") and "backfill_gex_profiles.py --today" in line
    assert om.EXPECTED_JOBS["gex_daily.log"] is True
