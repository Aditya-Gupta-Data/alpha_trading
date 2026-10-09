"""Audit Chunk 5, Batch B: Q3 deals report date, Q4 stale-LTP refusal, Q6 unusable score = stale, Q8 absent strike abstains,
F2 renewal retries + page, F4 DH-906, F5 refused quotes print their code, F7 macro_nightly heartbeat, F8 Gemini line."""
from src import notifier, ops_monitor as om


def test_q4_a_quote_far_off_ltp_refuses_the_leg_and_a_missing_side_still_falls_to_ltp():
    from src.options_proposer import _leg_fill
    assert _leg_fill({"oc": {"100.000000": {"pe": {"top_bid_price": 12, "last_price": 30}}}}, 100.0, "pe", "SELL") == (None, None)
    assert _leg_fill({"oc": {"100.000000": {"pe": {"last_price": 30}}}}, 100.0, "pe", "SELL") == (30.0, "ltp")
    assert _leg_fill({"oc": {"100.000000": {"pe": {"top_bid_price": 28, "last_price": 30}}}}, 100.0, "pe", "SELL") == (28.0, "quoted")


def test_q3_deals_carry_their_report_date_not_the_run_date():
    from src.ingestion.deals_tracker import report_day
    rows = [{"BD_DT_DATE": "03-Oct-2026", "symbol": "X"}, {"BD_DT_DATE": "03-Oct-2026", "symbol": "Y"},
            {"date": "02-Oct-2026", "symbol": "Z"}]
    assert report_day(rows) == "2026-10-03"
    assert report_day([{"symbol": "X"}]) is None and report_day(None) is None


def test_q6_an_unusable_score_is_a_stale_neutral_never_a_fresh_zero():
    from src.news_processor import _clean_entry
    e = _clean_entry({"short_term_catalyst_score": "bullish"}, "2026-10-09T10:00:00")
    assert e["short_term_catalyst_score"] == 0 and e["stale"] is True
    e = _clean_entry({"short_term_catalyst_score": 3}, "2026-10-09T10:00:00")
    assert e["short_term_catalyst_score"] == 3 and e["stale"] is False
    assert _clean_entry({}, "x")["stale"] is True


def test_q8_an_absent_strike_abstains_instead_of_marking_the_long_at_zero():
    from src.execution.live_pricer import crossed_mark
    chain = {"oc": {"24000.000000": {"pe": {"top_bid_price": 50, "top_ask_price": 51, "last_price": 50.5}}}}
    legs = [{"side": "BUY", "strike": 24100.0, "option_type": "PE"}, {"side": "SELL", "strike": 24000.0, "option_type": "PE"}]
    m = crossed_mark(chain, legs)
    assert not m["ok"] and "absent" in m["reason"]
    chain["oc"]["24100.000000"] = {"pe": {"top_ask_price": 80, "last_price": 79}}     # present, no bid: long = 0
    assert crossed_mark(chain, legs)["ok"]


def test_f2_renewal_retries_then_pages(monkeypatch):
    from src import renew_token as rt
    calls, cards = [], []
    monkeypatch.setattr(rt, "ENV_PATH", type("P", (), {"exists": lambda s: True, "read_text": lambda s: "DHAN_CLIENT_ID=x\n"})())
    monkeypatch.setattr(rt, "v2_ready", lambda c: True)
    def _transport_fail(env, creds):
        calls.append(1); rt._LAST_FAILURE = "transport"; return 1
    monkeypatch.setattr(rt, "renew_v2", _transport_fail)
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    monkeypatch.setattr("src.notifier.fire_broadcast", lambda p: cards.append(p))
    assert rt.renew() == 1 and len(calls) == 3 and cards[0]["event"] == "token_renewal_failed"
    assert "token_renewal_failed" in notifier.BUDGET_ALWAYS
    calls.clear(); cards.clear()
    monkeypatch.setattr(rt, "renew_v2", lambda env, creds: 0)
    assert rt.renew() == 0 and cards == []


def test_f4_f7_f8_ops_wording():
    from src import ceo_brief
    assert "token" in ceo_brief.DHAN_ERROR_CODES["DH-906"]
    assert "macro_nightly.log" in om.EXPECTED_JOBS
    assert om.is_problem_line("  GEMINI_API_KEY not set — sentiment feed unavailable; writing neutral (stale).")
    assert om.is_problem_line("  Dhan quote refused for NIFTY 50: DH-902")
