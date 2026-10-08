"""Audit Chunk 4, Batch C: partial marks said (T2), flat = realized (T10), crossed capture exposed (T5)."""
from src.dashboard import data as d


def test_t2_t10_net_equity_states_partial_marks_and_flat_books():
    a = d._with_mtm({"equity": 1000.0}, {"unrealized_pnl": 50.0, "marked_positions": 15, "open_positions": 16}, "x")
    assert a["net_equity"] == 1050.0 and a["net_equity_partial"] and a["marked_of"] == "15/16"
    a = d._with_mtm({"equity": 1000.0}, {"unrealized_pnl": 50.0, "marked_positions": 2, "open_positions": 2}, "x")
    assert a["net_equity"] == 1050.0 and not a["net_equity_partial"]
    a = d._with_mtm({"equity": 1000.0}, {"unrealized_pnl": None, "marked_positions": 0, "open_positions": 0}, "x")
    assert a["net_equity"] == 1000.0 and not a["net_equity_partial"]
    a = d._with_mtm({"equity": 1000.0}, {"unrealized_pnl": None, "marked_positions": 0, "open_positions": 3}, "x")
    assert a["net_equity"] is None


def test_t5_open_rows_carry_the_crossed_capture_and_its_basis(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(d, "JOURNAL_PATH", tmp_path / "j.jsonl")
    (tmp_path / "j.jsonl").write_text(json.dumps({
        "short_id": "ab12cd34", "ticker": "NIFTY 50", "date": "2026-10-01", "decision": "approved", "outcome": None,
        "spread": {"strategy": "bear_put_spread", "direction": "bearish", "expiry": "2026-10-27", "lots": 1,
                   "max_loss": 100.0, "legs": []}, "ratchet": {"peak_capture_pct": 29.0, "locked_pct": None}}) + "\n")
    rows = d.open_trades(snapshot_marks={"ab12cd34": {"live_pnl_rs": 10.0, "capture_pct": 70.0, "real_capture_pct": 29.0}},
                         live=[], now=None, captured_at=None)
    r = [x for x in rows if x["id"] == "ab12cd34"][0]
    assert r["capture_pct"] == 70.0 and r["real_capture_pct"] == 29.0 and r["capture_basis"] == "crossed"
