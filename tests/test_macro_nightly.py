"""
The VM macro heartbeat, fully offline: all three stages run through
injected fns, each stage fails open independently (a dead stage never
aborts the others or raises), and one heartbeat line is written.
"""
import json
from datetime import date

from src.analysis import macro_nightly as MN


def test_run_drives_all_three_stages_and_writes_heartbeat(tmp_path):
    hb = tmp_path / "hb.log"
    out = MN.run(
        fred_fn=lambda: {"ok": ["BRENT", "DXY"], "failed": []},
        indices_fn=lambda d: {"no_file": False, "rows_added": {"NIFTY": 1}},
        declare_fn=lambda: {"declared": True, "horizons": {
            "shock": {"declared": True, "phase": "P3_resolution",
                      "best": {"archetype": "A2"}}}},
        clock=lambda: date(2026, 7, 23), heartbeat_path=hb,
        notify_fn=lambda p: None)
    assert out["as_of"] == "2026-07-23"
    assert out["stages"]["fred"]["ok"] == ["BRENT", "DXY"]
    assert out["stages"]["indices"]["rows_added"] == 1
    assert out["stages"]["declare"]["declared"] is True
    assert out["stages"]["declare"]["horizons"]["shock"]["archetype"] == "A2"
    # exactly one heartbeat line, valid JSON
    lines = hb.read_text().strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["as_of"] == "2026-07-23"


def test_a_dead_stage_never_aborts_the_others(tmp_path):
    def boom():
        raise RuntimeError("FRED key missing")
    out = MN.run(
        fred_fn=boom,                                   # FRED explodes
        indices_fn=lambda d: {"no_file": True, "rows_added": {}},
        declare_fn=lambda: {"declared": False, "horizons": {}},
        clock=lambda: date(2026, 7, 23), heartbeat_path=tmp_path / "hb.log",
        notify_fn=lambda p: None)
    assert "error" in out["stages"]["fred"]             # named, not raised
    assert out["stages"]["indices"]["no_file"] is True  # still ran
    assert out["stages"]["declare"]["declared"] is False
    assert (tmp_path / "hb.log").exists()               # heartbeat still fired


def test_declare_failure_is_isolated(tmp_path):
    def boom():
        raise ValueError("templates artifact missing")
    out = MN.run(
        fred_fn=lambda: {"ok": ["BRENT"], "failed": []},
        indices_fn=lambda d: {"no_file": False, "rows_added": {"NIFTY": 1}},
        declare_fn=boom,
        clock=lambda: date(2026, 7, 23), heartbeat_path=tmp_path / "hb.log",
        notify_fn=lambda p: None)
    assert out["stages"]["fred"]["ok"] == ["BRENT"]
    assert "error" in out["stages"]["declare"]


def test_stage_b_scorer_runs_as_stage_four(tmp_path):
    """SB-2: the forward scorer runs after declare and its summary is recorded."""
    out = MN.run(
        fred_fn=lambda: {"ok": ["BRENT"], "failed": []},
        indices_fn=lambda d: {"no_file": False, "rows_added": {"NIFTY": 1}},
        declare_fn=lambda: {"declared": True, "horizons": {}},
        scorer_fn=lambda: {"graded": 2, "wins": 1, "pending": 5,
                           "confirmed": 0, "contradicted": 0},
        clock=lambda: date(2026, 7, 23), heartbeat_path=tmp_path / "hb.log",
        notify_fn=lambda p: None)
    assert out["stages"]["score"]["graded"] == 2
    assert out["stages"]["score"]["pending"] == 5


def test_stage_b_scorer_failure_never_aborts_the_clock(tmp_path):
    """SB-2 fail-open: a scorer fault is named, and the declaration still ran."""
    def boom():
        raise RuntimeError("scores ledger unreadable")
    out = MN.run(
        fred_fn=lambda: {"ok": ["BRENT"], "failed": []},
        indices_fn=lambda d: {"no_file": False, "rows_added": {"NIFTY": 1}},
        declare_fn=lambda: {"declared": True, "horizons": {}},
        scorer_fn=boom,
        clock=lambda: date(2026, 7, 23), heartbeat_path=tmp_path / "hb.log",
        notify_fn=lambda p: None)
    assert "error" in out["stages"]["score"]             # named, not raised
    assert out["stages"]["declare"]["declared"] is True  # declaration untouched
    assert (tmp_path / "hb.log").exists()


# ------------------------------------------------- the Discord heartbeat card

def _stages_all_green():
    return {"fred": {"ok": ["BRENT"], "failed": []},
            "indices": {"no_file": False, "rows_added": 2},
            "declare": {"declared": True, "horizons": {}},
            "score": {"graded": 0, "wins": 0}}


def test_heartbeat_line_all_green():
    line, all_ok = MN.heartbeat_line(_stages_all_green())
    assert line == "[🟢 FRED: OK | 🟢 Indices: OK | 🟢 Declare: OK | 🟢 Scorer: OK]"
    assert all_ok is True


def test_heartbeat_line_marks_each_failed_open_stage_red():
    stages = _stages_all_green()
    stages["indices"] = {"error": "RuntimeError: NSE archive 500"}
    line, all_ok = MN.heartbeat_line(stages)
    assert line == "[🟢 FRED: OK | 🔴 Indices: FAILED | 🟢 Declare: OK | 🟢 Scorer: OK]"
    assert all_ok is False


def test_heartbeat_line_fred_failed_series_and_cache_miss_are_red():
    stages = _stages_all_green()
    stages["fred"] = {"ok": ["BRENT"], "failed": ["DXY", "VIX"]}
    stages["declare"] = {"declared": False, "horizons": {},
                         "ALERT": "Cache Miss/Stale - Aborting"}
    line, all_ok = MN.heartbeat_line(stages)
    assert "🔴 FRED: 2 series FAILED" in line
    assert "🔴 Declare: CACHE MISS" in line
    assert all_ok is False


def test_heartbeat_line_holiday_and_abstention_are_not_failures():
    stages = _stages_all_green()
    stages["indices"] = {"no_file": True, "rows_added": 0}
    stages["declare"] = {"declared": False, "horizons": {}}   # honest abstain
    line, all_ok = MN.heartbeat_line(stages)
    assert "🟢 Indices: OK (no file — holiday?)" in line
    assert "🟢 Declare: OK" in line
    assert all_ok is True


def test_run_fires_the_card_unconditionally_with_the_status_line(tmp_path):
    sent = []
    out = MN.run(
        fred_fn=lambda: {"ok": ["BRENT"], "failed": []},
        indices_fn=lambda d: {"no_file": False, "rows_added": {"NIFTY": 1}},
        declare_fn=lambda: {"declared": True, "horizons": {}},
        scorer_fn=lambda: {"graded": 0},
        clock=lambda: date(2026, 7, 23), heartbeat_path=tmp_path / "hb.log",
        notify_fn=sent.append)
    assert len(sent) == 1
    card = sent[0]
    assert card["event"] == "macro_heartbeat"
    assert card["date"] == "2026-07-23"
    assert card["all_ok"] is True
    assert card["description"] == out["status_line"]
    assert card["description"].startswith("[🟢 FRED: OK |")


def test_run_card_reflects_a_dead_stage_and_a_dead_discord_never_raises(tmp_path):
    sent = []

    def flaky_notify(payload):
        sent.append(payload)
        raise ConnectionError("webhook down")           # Discord outage

    def boom():
        raise RuntimeError("scores ledger unreadable")
    out = MN.run(
        fred_fn=lambda: {"ok": ["BRENT"], "failed": []},
        indices_fn=lambda d: {"no_file": False, "rows_added": {"NIFTY": 1}},
        declare_fn=lambda: {"declared": True, "horizons": {}},
        scorer_fn=boom,
        clock=lambda: date(2026, 7, 23), heartbeat_path=tmp_path / "hb.log",
        notify_fn=flaky_notify)                         # must not raise
    assert sent[0]["all_ok"] is False
    assert "🔴 Scorer: FAILED" in sent[0]["description"]
    # the heartbeat LOG line survived the Discord outage, with the same line
    logged = json.loads((tmp_path / "hb.log").read_text().strip())
    assert logged["status_line"] == sent[0]["description"]


def test_heartbeat_card_embed_is_titled_and_coloured_by_health():
    from src import notifier as N
    green = N._build_embed({"event": "macro_heartbeat", "ticker": "MACRO",
                            "date": "2026-07-25", "all_ok": True,
                            "description": "[🟢 FRED: OK]"})
    assert green["title"].startswith("🫀 Macro Nightly Heartbeat")
    assert green["description"] == "[🟢 FRED: OK]"
    assert green["color"] == N._COLOUR["macro_heartbeat"]
    red = N._build_embed({"event": "macro_heartbeat", "ticker": "MACRO",
                          "date": "2026-07-25", "all_ok": False,
                          "description": "[🔴 FRED: FAILED]"})
    assert red["color"] == N._COLOUR_LOSS


def test_heartbeat_event_is_budget_scheduled(tmp_path):
    from src import notifier as N
    assert "macro_heartbeat" in N.BUDGET_SCHEDULED
    verdict = N.budget_gate({"event": "macro_heartbeat"},
                            state_path=tmp_path / "budget.json",
                            queue_path=tmp_path / "queue.jsonl",
                            enabled=True)
    assert verdict == "send"
