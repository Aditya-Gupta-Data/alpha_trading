"""Pipeline wiring (Architect 2026-10-28, #159): the event engine spends no LLM call on a feed item that
cannot reach the VM's option universe, and the session playbook's planner gets the previous session's
calibrated GEX read and the session move, so its D4 zone and D6 routing apply. Offline."""
from datetime import date
from pathlib import Path

from src import lake, master_scheduler as ms
from src.event_engine import detect, emit
from tests.test_event_engine import CFG, FakeExtractor
from src.event_engine.llm import EventLLM


# --------------------------------------------------------------- pre-LLM universe filter

def test_only_items_that_can_reach_the_universe_go_to_the_llm():
    assert detect.in_universe({"title": "Infosys wins a $2bn deal", "text": "", "source": "rss:x"})
    assert detect.in_universe({"title": "RBI cuts repo rate by 25 bps", "text": "", "source": "rss:x"})
    assert detect.in_universe({"title": "x", "text": "Reliance Industries to demerge", "source": "rss:x"})
    assert not detect.in_universe({"title": "CONCOR raises freight rates", "text": "rail haulage", "source": "rss:x"})
    assert not detect.in_universe({"title": "Adani Green wins solar PLI", "text": "module capacity", "source": "rss:x"})
    assert not detect.in_universe({"title": "Nippon AMC settles", "text": "", "source": "rss:x"})
    assert detect.in_universe({"title": "CONCOR", "text": "", "source": "inbox:concor.md"})   # the owner's choice


def test_an_item_outside_the_universe_spends_no_llm_call_and_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(emit, "STATE_DIR", tmp_path / "state")
    fx = FakeExtractor()
    llm = EventLLM(extractor=fx, cache_dir=tmp_path / "c", ledger=tmp_path / "l.jsonl", cap=100, run_cap=50)
    seen = detect.SeenLedger(tmp_path / "seen.txt")
    item = {"title": "CONCOR raises freight rates", "text": "x" * 200, "url": "u", "source": "rss:et"}
    s = emit.run(items=[item], llm=llm, card_dir=tmp_path / "cards", do_ship=False, seen=seen, cfg=dict(CFG))
    assert fx.calls == 0 and s["skipped"] == {"outside the VM option universe (pre-LLM)": 1}
    assert detect.item_id(item) in seen


# --------------------------------------------------------------- GEX into the playbook

def _gex_row(regime, zero_gamma, calibrated=True):
    cal = {"non_client": {"regime_at_spot": regime, "zero_gamma": zero_gamma}} if calibrated else {}
    return {"combined": {"repriced": {"regime_at_spot": "positive", "zero_gamma": 1.0}, "calibrated": cal}}


def test_latest_gex_reads_the_newest_calibrated_profile_before_today(tmp_path):
    lake.write_partition("gex/infy", "2026-10-26", [_gex_row("negative", 1142.06)], root=tmp_path)
    lake.write_partition("gex/infy", "2026-10-27", [_gex_row(None, None, calibrated=False)], root=tmp_path)
    lake.write_partition("gex/infy", "2026-10-28", [_gex_row("positive", 1200.0)], root=tmp_path)
    got = ms.latest_gex("INFY.NS", today=date(2026, 10, 28), root=tmp_path)
    assert got == {"zero_gamma": 1142.06, "regime_at_spot": "negative", "day": "2026-10-26"}   # 10-27 uncalibrated
    assert ms.latest_gex("INFY.NS", today=date(2026, 11, 2), root=tmp_path) == {}                # all too old
    assert ms.latest_gex("SBIN.NS", today=date(2026, 10, 28), root=tmp_path) == {}               # not archived


def test_the_session_move_is_the_live_price_against_the_previous_close():
    assert ms._session_move_pct({"closes": [100.0, 99.0]}) == -1.0
    assert ms._session_move_pct({"closes": [100.0]}) is None and ms._session_move_pct({}) is None


def test_the_playbook_routes_a_short_gamma_zone_through_d6():
    state = {"analysis": {"ticker": "NIFTY BANK", "uptrend": True, "fresh_cross": False, "rsi": 55.0,
                          "price": 52_340.0, "closes": [53_000.0, 52_340.0]}, "vix": 14.5}
    gex = {"zero_gamma": 54_000.0, "regime_at_spot": "negative", "day": "2026-10-27"}
    line = ms._playbook_lines(("NIFTY BANK",), fetch_fn=lambda u: state, gex_fn=lambda u: gex)[0]
    assert "bear put spread" in line and "view momentum_bearish" in line           # -1.25% session: D6 momentum
    assert "GEX short gamma vs S* 54,000 (2026-10-27)" in line
    none = ms._playbook_lines(("NIFTY BANK",), fetch_fn=lambda u: state, gex_fn=lambda u: {})[0]
    assert "iron condor" in none and "GEX no calibrated read" in none               # no read: the SMA matrix


def test_the_vm_schedule_refreshes_gex_before_the_session():
    text = (Path(__file__).resolve().parent.parent / "scripts" / "setup_cron.sh").read_text()
    line = next(ln for ln in text.splitlines() if ln.startswith("50 8 * * 1-5 "))
    assert "participant_oi --today" in line and "backfill_gex_profiles.py --today" in line
