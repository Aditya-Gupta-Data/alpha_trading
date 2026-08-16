"""Insolvency Short (src/strategies/insolvency_short.py) — Week-1 release,
SHADOW mode. Offline: injected filings, injected F&O list, temp ledger."""
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.strategies import insolvency_short as ins

FO = {"as_of": "2026-08-14", "banned": ["BANNEDCO"],
      "symbols": {"TIER1CO": {"tier": "tier1"},
                  "FUTONLY": {"tier": "tier2"},
                  "BANNEDCO": {"tier": "tier1"}}}

ROWS = [
    {"as_of": "2026-08-14", "symbol": "TIER1CO",
     "subject": "Corporate Insolvency Resolution Process"},
    {"as_of": "2026-08-14", "symbol": "PENNYCO",
     "subject": "Defaults on Payment"},
    {"as_of": "2026-08-14", "symbol": "FUTONLY",
     "subject": "defaults on payment"},                     # case-blind
    {"as_of": "2026-08-14", "symbol": "TIER1CO",
     "subject": "Insolvency proceedings against a customer"},  # substring ≠ trigger
    {"as_of": "2026-08-14", "symbol": "TIER1CO", "subject": "Board Meeting"},
]


def test_triggers_are_the_two_exact_sebi_subjects_only():
    trig = ins.scan_triggers("2026-08-14", rows=ROWS)
    assert [(t["symbol"], t["subject"].lower()) for t in trig] == [
        ("TIER1CO", "corporate insolvency resolution process"),
        ("PENNYCO", "defaults on payment"),
        ("FUTONLY", "defaults on payment")]
    assert ins.is_trigger("Insolvency") is False       # substring is NOT enough


def test_an_unreadable_lake_yields_no_triggers_not_a_crash():
    def boom(day):
        raise OSError("lake gone")
    assert ins.scan_triggers("2026-08-14", read_day_fn=boom) == []


def test_fo_gate_rejects_non_fo_futures_only_and_banned_names():
    assert ins.fo_gate("PENNYCO", fo=FO) == (False, "not_in_fo")
    assert ins.fo_gate("FUTONLY", fo=FO) == (False, "no_listed_options")
    assert ins.fo_gate("BANNEDCO", fo=FO) == (False, "fo_banned")
    assert ins.fo_gate("TIER1CO", fo=FO) == (True, "fo_tier1")
    assert ins.fo_gate("TIER1CO", fo=None, path="/nonexistent/fo.json") == (
        False, "fo_list_unavailable")                     # fail-CLOSED


def test_sizing_is_floor_of_half_a_percent_never_rounded_up():
    assert ins.RISK_PCT_PER_SETUP == 0.005
    assert ins.lots_for(200_000, 250.0) == 4              # 1,000 / 250
    assert ins.lots_for(200_000, 1_001.0) == 0            # one lot over the cap
    assert ins.lots_for(200_000, 0) == 0
    assert ins.lots_for("x", 10) == 0


def _trigger(sym="TIER1CO"):
    return {"date": "2026-08-14", "symbol": sym,
            "subject": "Corporate Insolvency Resolution Process"}


def test_a_setup_is_a_bear_put_spread_capped_at_half_a_percent_with_a_day5_exit():
    s = ins.build_setup(_trigger(), spot=1000.0, buy_strike=1000.0,
                        sell_strike=980.0, buy_premium=30.0, sell_premium=22.0,
                        lot_size=100, expiry="2026-08-27",
                        pool_rupees=200_000.0, fo=FO)
    assert s["accepted"] is True and s["mode"] == "shadow"
    sp = s["spread"]
    assert sp["strategy"] == "bear_put_spread" and sp["direction"] == "bearish"
    assert [l["side"] for l in sp["legs"]] == ["BUY", "SELL"]
    assert all(l["option_type"] == "PE" for l in sp["legs"])
    # net debit 8 × lot 100 = Rs.800 max loss per lot; 0.5% of 2L = 1,000 → 1 lot
    assert sp["max_loss"] == 800.0 and s["lots"] == 1
    assert s["risk_rupees"] == 800.0 and s["risk_pct_of_pool"] <= 0.5
    assert s["hold_sessions"] == 5
    assert s["time_exit_on"] == "2026-08-21"             # Fri 14th + 5 weekdays
    assert "time exit at session +5" in s["exit_rule"]


def test_a_non_fo_trigger_is_rejected_before_any_spread_is_built():
    s = ins.build_setup(_trigger("PENNYCO"), spot=10.0, buy_strike=10.0,
                        sell_strike=9.0, buy_premium=1.0, sell_premium=0.5,
                        lot_size=1000, expiry="2026-08-27",
                        pool_rupees=200_000.0, fo=FO)
    assert s["accepted"] is False and s["reason"] == "not_in_fo"
    assert "spread" not in s


def test_a_spread_whose_one_lot_exceeds_the_cap_gets_zero_lots_and_is_rejected():
    s = ins.build_setup(_trigger(), spot=1000.0, buy_strike=1000.0,
                        sell_strike=900.0, buy_premium=60.0, sell_premium=20.0,
                        lot_size=100, expiry="2026-08-27",
                        pool_rupees=200_000.0, fo=FO)     # 40 × 100 = 4,000 > 1,000
    assert s["accepted"] is False and s["reason"].startswith("risk_cap")


def test_an_incoherent_spread_is_refused_not_inverted():
    s = ins.build_setup(_trigger(), spot=1000.0, buy_strike=980.0,
                        sell_strike=1000.0, buy_premium=22.0, sell_premium=30.0,
                        lot_size=100, expiry="2026-08-27",
                        pool_rupees=200_000.0, fo=FO)
    assert s["accepted"] is False and s["reason"] == "spread_incoherent"


def test_run_is_shadow_only_and_records_the_gate_verdicts(tmp_path):
    fo_path = tmp_path / "fo.json"
    fo_path.write_text(json.dumps(FO))
    ledger = tmp_path / "shadow.jsonl"
    quotes = {"TIER1CO": dict(spot=1000.0, buy_strike=1000.0, sell_strike=980.0,
                              buy_premium=30.0, sell_premium=22.0,
                              lot_size=100, expiry="2026-08-27")}
    r = ins.run("2026-08-14", rows=ROWS, pool_rupees=200_000.0,
                fo_path=fo_path, quote_fn=lambda s: quotes.get(s),
                ledger_path=ledger)
    assert r["mode"] == "shadow" and r["triggers"] == 3
    assert [s["symbol"] for s in r["setups"]] == ["TIER1CO"]
    assert sorted((x["symbol"], x["reason"]) for x in r["rejected"]) == [
        ("FUTONLY", "no_listed_options"), ("PENNYCO", "not_in_fo")]
    rows = [json.loads(l) for l in ledger.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["day"] == "2026-08-14"


def test_without_a_chain_reader_the_pass_still_logs_but_builds_nothing(tmp_path):
    fo_path = tmp_path / "fo.json"; fo_path.write_text(json.dumps(FO))
    r = ins.run("2026-08-14", rows=ROWS, fo_path=fo_path,
                ledger_path=tmp_path / "s.jsonl")
    assert r["setups"] == []
    assert any(x["reason"] == "no_chain_reader_injected" for x in r["rejected"])


def test_the_module_touches_no_journal_treasury_or_cron():
    src = Path("src/strategies/insolvency_short.py").read_text()
    for forbidden in ("journal.log(", "firm_treasury", "plan_tracker.open",
                      "portfolio_manager", "paper_broker"):
        assert forbidden not in src
    assert "insolvency_short" not in Path("scripts/setup_cron.sh").read_text()
    assert ins.MODE == "shadow"
