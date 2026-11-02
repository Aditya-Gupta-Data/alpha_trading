"""scripts/ratchet_near_miss_study.py — the read-only ratchet near-miss study."""
import importlib.util
import json
import sqlite3
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "ratchet_near_miss_study", Path(__file__).resolve().parent.parent / "scripts" / "ratchet_near_miss_study.py")
rs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rs)


def _row(sid, peak, pnl, strategy="bear_put_spread", decision="approved", hypothetical=False,
         frictions=100.0, open_=False):
    return {"short_id": sid, "ticker": "TCS.NS", "date": "2026-09-24", "decision": decision,
            "spread": {"strategy": strategy}, "ratchet": {"peak_capture_pct": peak},
            "outcome": None if open_ else {"resolution": "pre_expiry_exit", "pct": -30.0, "pnl_rs": pnl,
                                           "frictions_rs": frictions, "hypothetical": hypothetical}}


def _journal(tmp_path, rows):
    p = tmp_path / "journal.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    return p


def test_loader_keeps_only_settled_real_directional_rows_and_names_skips(tmp_path):
    p = _journal(tmp_path, [
        _row("a", 38.0, -11000.0),
        _row("b", 30.0, 2500.0, strategy="bull_call_spread"),
        _row("c", 50.0, 5000.0, strategy="iron_condor"),      # not directional: ignored
        _row("d", 25.0, -100.0, decision="rejected"),           # never opened: ignored
        _row("e", 25.0, -100.0, hypothetical=True),
        _row("f", 25.0, None, open_=True),
        {"short_id": "g", "decision": "approved", "spread": {"strategy": "bear_put_spread"},
         "ratchet": {}, "outcome": {"pnl_rs": -5.0}},
    ])
    rows, skipped = rs.load_journal_rows(p)
    assert [r["ref"] for r in rows] == ["a", "b"]
    assert skipped == {"unparseable line": 1, "hypothetical outcome": 1, "still open": 1,
                       "no ratchet peak on the row": 1}


def test_bands_and_what_if_give_an_honest_range():
    rows = [{"peak": p, "pnl": pnl, "frictions": 100.0} for p, pnl in
            [(-5.0, -3000.0), (15.0, -2000.0), (38.0, -11000.0), (32.0, -4000.0),
             (27.0, 1500.0), (45.0, 6000.0)]]
    by = {b["band"]: b for b in rs.bands_table(rows)}
    assert by["never positive"]["n"] == 1 and by["30-40%"]["losers"] == 2
    assert by["20-30%"]["winners"] == 1 and by["40%+ (armed today)"]["pnl"] == 6000.0
    w = rs.what_if(rows, 25.0, 40.0)
    # losers 38% and 32% rescued at breakeven (-frictions each): saved = 10900 + 3900
    assert w["losers"] == 2 and w["saved_best"] == 14800.0
    # the 27% winner may have been cut at breakeven: worst cost = 1500 + 100
    assert w["winners"] == 1 and w["cost_worst"] == 1600.0
    assert (w["net_low"], w["net_high"]) == (13200.0, 14800.0) and w["thin"] is True
    # rows already armed (>= 40%) and below the new rung never change
    assert rs.what_if(rows, 39.0, 40.0)["affected"] == 0


def test_live_rows_read_only_and_render_names_everything(tmp_path):
    db = tmp_path / "b.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE paper_live_positions (account_id, journal_ref, ticker, strategy, opened_at, "
              "resolution, ratchet_peak_pct, last_capture_pct, pnl_net, frictions_rs, state)")
    c.executemany("INSERT INTO paper_live_positions VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
        ("PAPER_2L_LIVE", "r1", "NIFTY BANK", "bear_put_spread", "2026-10-26T10:00", "pre_expiry_exit",
         24.3, -24.8, -1280.0, 60.0, "closed"),
        ("PAPER_2L_LIVE", "r2", "NIFTY 50", "iron_butterfly", "2026-10-26T10:00", "x", 10.0, 1.0, 5.0, 1.0, "closed"),
        ("PAPER_2L_LIVE", "r3", "NIFTY 50", "bear_put_spread", "2026-10-26T10:00", None, 5.0, 1.0, None, 1.0, "open"),
    ])
    c.commit()
    c.close()
    live, sk = rs.load_live_rows(db)
    assert [r["ref"] for r in live] == ["r1"] and sk == {}
    text = rs.render({"PAPER_2L_LIVE (crossed quotes)": live, "empty": []}, [20.0, 40.0, 45.0],
                     {"still open": 3}, 40.0)
    assert "| 20% | 1 ⚠️ n<20 | 1 | 0 |" in text
    assert "| 40% |" not in text                       # a rung at/above today's arm is not a what-if
    assert "r1" in text and "Nothing to read." in text and "- still open: 3" in text
    assert rs.load_live_rows(tmp_path / "missing.db")[0] == []


def test_main_writes_only_where_told(tmp_path, capsys):
    p = _journal(tmp_path, [_row("a", 38.0, -11000.0)])
    out = tmp_path / "r.md"
    assert rs.main(["--journal", str(p), "--out", str(out)]) == 0
    assert "Ratchet near-miss study" in out.read_text()
    assert rs.main(["--journal", str(p)]) == 0
    assert "| 30-40% | 1 ⚠️ n<20 | 1 | 0 |" in capsys.readouterr().out
