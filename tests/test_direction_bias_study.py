"""scripts/direction_bias_study.py — the read-only direction bias study."""
import importlib.util
import json
import sqlite3
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "direction_bias_study", Path(__file__).resolve().parent.parent / "scripts" / "direction_bias_study.py")
db = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(db)


def _e(strategy, pnl, date="2026-10-05", direction=None, decision="approved", hyp=False, rr=None):
    s = {"strategy": strategy}
    if direction:
        s["direction"] = direction
    e = {"date": date, "decision": decision, "spread": s,
         "outcome": None if pnl is None else {"pnl_rs": pnl, "hypothetical": hyp}}
    if rr:
        e["regime_read"] = rr
    return e


def test_settled_trades_scoreboard_by_direction_and_month():
    rows = [_e("bear_put_spread", -1000.0), _e("bear_put_spread", 500.0, date="2026-09-20"),
            _e("bull_call_spread", 2000.0), _e("iron_condor", 300.0, direction="neutral"),
            _e("bear_put_spread", -9.0, decision="rejected"), _e("bear_put_spread", -9.0, hyp=True),
            _e("bear_put_spread", None), _e("mystery", 10.0)]
    t = db.settled_trades(rows)
    assert len(t) == 5
    by = {b["group"]: b for b in db.scoreboard(t)}
    assert by["bearish"]["n"] == 2 and by["bearish"]["win_rate"] == 50.0 and by["bearish"]["pnl"] == -500.0
    assert by["bullish"]["avg"] == 2000.0 and by["unknown"]["n"] == 1
    md = {b["group"] for b in db.scoreboard([dict(x, md=f"{x['month']} {x['direction']}") for x in t], key="md")}
    assert "2026-09 bearish" in md and "2026-10 bearish" in md


def test_classifier_and_ledger_mix_count_the_bullish_veto():
    rows = [_e("bear_put_spread", None, rr={"view": "bearish", "grade": "bearish"}),
            _e("bear_put_spread", None, decision="rejected", rr={"view": "bearish", "grade": "bearish"}),
            _e("bear_put_spread", None, rr={"view": "bearish", "grade": "strong_bearish"}),
            _e("bear_put_spread", None)]
    assert db.classifier_mix(rows) == {("bearish", "bearish"): 2, ("bearish", "strong_bearish"): 1}
    ledger = [{"direction": "bearish", "fate": "EXECUTED"},
              {"strategy": "bull_call_spread", "fate": "REJECTED_OTHER",
               "reason": "smart-money/sector veto: FII selling; sector bearish"},
              {"fate": "REJECTED_OTHER", "reason": "advisory: smart-money/sector veto: x"},
              {"fate": "REJECTED_POOR_RR", "reason": "rr 1.1 < 1.5"}]
    mix, vetoes, n = db.ledger_mix(ledger)
    assert vetoes == 2 and n == 4
    assert mix[("bearish", "EXECUTED")] == 1 and mix[("bullish", "REJECTED_OTHER")] == 1
    assert mix[("unknown", "REJECTED_OTHER")] == 1


def test_live_books_and_render(tmp_path):
    p = tmp_path / "b.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE paper_live_positions (account_id, strategy, direction, opened_at, pnl_net, state)")
    c.executemany("INSERT INTO paper_live_positions VALUES (?,?,?,?,?,?)", [
        ("PAPER_SHADOW_LEARNER", "bear_put_spread", "bearish", "2026-10-12T10:00", -1500.0, "closed"),
        ("PAPER_SHADOW_LEARNER", "bull_call_spread", None, "2026-10-12T10:00", 800.0, "closed"),
        ("PAPER_2L_LIVE", "bear_put_spread", "bearish", "2026-10-12T10:00", None, "closed"),
        ("PAPER_2L_LIVE", "bear_put_spread", "bearish", "2026-10-12T10:00", -5.0, "open"),
    ])
    c.commit()
    c.close()
    live, err = db.live_trades(p)
    assert err is None and len(live) == 2 and {t["direction"] for t in live} == {"bearish", "bullish"}
    assert db.live_trades(tmp_path / "nope" / "x.db")[1] is not None
    text = db.render([_e("bear_put_spread", -1000.0, rr={"view": "bearish", "grade": "bearish"})],
                     [{"fate": "REJECTED_OTHER", "reason": "smart-money/sector veto: y"}], live, ["note one"])
    assert "| bearish | 1 ⚠️ n<20 | 0 | 0.0% | -Rs.1,000 | -Rs.1,000 |" in text
    assert "Bearish views: 1 MILD" in text and "advisory: 1." in text
    assert "**PAPER_SHADOW_LEARNER**" in text and "- note one" in text


def test_main_names_missing_inputs_and_writes_only_where_told(tmp_path):
    out = tmp_path / "r.md"
    assert db.main(["--journal", str(tmp_path / "none.jsonl"), "--ledger", str(tmp_path / "none2.jsonl"),
                    "--out", str(out)]) == 0
    text = out.read_text()
    assert "journal empty or unreadable" in text and "No settled trades found." in text
