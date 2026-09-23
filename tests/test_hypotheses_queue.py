"""Decision #109 — the Proving Court's hypothesis queue: ToD entry window
(Hypothesis A) and Mansfield RS (Hypothesis B), scored nightly, never live."""
import sqlite3
from datetime import date

import pytest

from src.validation import hypotheses as hyp
from src.validation.hypotheses import _metrics as m, mansfield_rs as rs, tod_entry_window as tod


def test_metrics_sharpe_and_max_drawdown():
    assert m.per_trade_sharpe([1.0, 1.0, 1.0]) is None                  # zero dispersion
    assert m.per_trade_sharpe([2.0, 0.0]) == pytest.approx(1.0 / (2 ** 0.5), abs=1e-4)
    assert m.max_drawdown([1.0, -0.5, -0.5, 2.0]) == 1.0                # 1 -> 0 -> +2
    assert m.max_drawdown([-1.0, -1.0]) == 2.0 and m.max_drawdown([]) is None
    s = m.summarize([1.0, -1.0, 2.0])
    assert s["n"] == 3 and s["win_rate"] == pytest.approx(2 / 3, abs=1e-4) and s["mean_r"] == pytest.approx(0.6667, abs=1e-4)


def _spread_entry(short_id, created_at, r, decision="approved"):
    return {"short_id": short_id, "decision": decision, "created_at": created_at,
            "spread": {"strategy": "bear_put_spread"}, "outcome": {"r_multiple": r}}


def test_tod_cohorts_split_on_the_entry_stamp():
    entries = [_spread_entry("a", "2026-09-01T09:20:00+05:30", -1.0),
               _spread_entry("b", "2026-09-01T10:00:00+05:30", 1.5),
               _spread_entry("c", "2026-09-02T14:30:00+05:30", 0.5),
               _spread_entry("d", "2026-09-02T14:31:00+05:30", -0.5),
               _spread_entry("e", None, 2.0),
               _spread_entry("f", "2026-09-03T11:00:00+05:30", 1.0, decision="rejected"),
               {"short_id": "g", "decision": "approved", "spread": {}, "outcome": None}]
    c = tod.cohorts(entries)
    assert c["in_window"] == [1.5, 0.5] and c["all_day"] == [-1.0, 1.5, 0.5, -0.5, 2.0]
    assert c["unstamped"] == 1
    assert tod.in_window("10:00") and tod.in_window("14:30") and not tod.in_window("14:31")


def test_tod_evidence_is_insufficient_below_min_n_and_leans_only_past_it():
    few = [_spread_entry(str(i), "2026-09-01T11:00:00+05:30", 1.0 if i % 2 else -0.4) for i in range(6)]
    assert tod.evidence(entries=few)["verdict"] == "insufficient_n"
    many = ([_spread_entry(f"i{i}", "2026-09-01T11:00:00+05:30", 1.0 + (i % 3) * 0.1) for i in range(25)]
            + [_spread_entry(f"o{i}", "2026-09-01T09:20:00+05:30", -1.0 + (i % 3) * 0.1) for i in range(25)])
    ev = tod.evidence(entries=many, min_n=20)
    assert ev["verdict"] == "supports" and ev["in_window"]["n"] == 25 and ev["all_day"]["n"] == 50
    assert ev["in_window"]["sharpe"] > ev["all_day"]["sharpe"]


def test_mansfield_rs_sign_means_out_or_under_performing_the_index():
    idx = [100.0 + i * 0.1 for i in range(300)]
    out = [100.0 + i * 0.5 for i in range(300)]          # rising faster than the index
    under = [100.0 - i * 0.1 for i in range(300)]
    assert rs.mansfield_rs(out, idx) > 0 and rs.mansfield_rs(under, idx) < 0
    assert rs.mansfield_rs(idx, idx) == 0.0
    assert rs.mansfield_rs(out[:100], idx[:100]) is None       # < 253 aligned bars


def _days(n, start=date(2025, 1, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d = date.fromordinal(d.toordinal() + 1)
    return out


def test_rs_cohorts_filter_on_rs_at_entry_and_name_the_unscored():
    days = _days(400)
    nifty = {d: 100.0 + i * 0.1 for i, d in enumerate(days)}
    strong = [(d, 0, 0, 100.0 + i * 0.6) for i, d in enumerate(days)]
    weak = [(d, 0, 0, 100.0 - i * 0.05) for i, d in enumerate(days)]
    bars = {"STRONG.NS": strong, "WEAK.NS": weak}
    entry_day = days[380]
    events = []
    for i, (t, r) in enumerate((("STRONG.NS", 1.2), ("WEAK.NS", -1.0), ("NOBARS.NS", 0.3), ("STRONG.NS", -0.4))):
        events.append({"event": "entry", "id": f"e{i}", "ticker": t, "as_of": entry_day})
        events.append({"event": "exit", "id": f"e{i}", "kya_sikha_autopsy": {"r_multiple": r}})
    c = rs.cohorts(events, bars_fn=lambda t, d: bars.get(t), nifty=nifty)
    assert c["unfiltered"] == [1.2, -1.0, 0.3, -0.4]
    assert c["rs_filtered"] == [1.2, -0.4] and c["unscored"] == 1
    ev = rs.evidence(events=events, bars_fn=lambda t, d: bars.get(t), nifty_closes_fn=lambda: nifty)
    assert ev["verdict"] == "insufficient_n" and ev["rs_filtered"]["n"] == 2


def test_run_nightly_registers_both_as_candidates_idempotently_and_records_evidence():
    from src.validation import registry as rg
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    rg.ensure_schema(conn)
    out = hyp.run_nightly(conn, date(2026, 9, 23), entries=[], events=[],
                          bars_fn=lambda t, d: [], nifty_closes_fn=lambda: {})
    assert set(out) == {"tod_entry_window", "mansfield_rs"}
    for name, row in out.items():
        assert row["status"] == "CANDIDATE" and row["verdict"] == "insufficient_n"
        stats = rg.get(conn, row["pattern_id"])["oos_stats"]
        assert "insufficient_n" in stats and "2026-09-23" in stats
    again = hyp.run_nightly(conn, date(2026, 9, 24), entries=[], events=[],
                            bars_fn=lambda t, d: [], nifty_closes_fn=lambda: {})
    assert {r["pattern_id"] for r in again.values()} == {r["pattern_id"] for r in out.values()}
    assert len(rg.list_by_status(conn, "CANDIDATE")) == 2
    assert len(hyp.render_lines(out)) == 2


def test_one_broken_hypothesis_is_a_named_skip_not_a_crash(monkeypatch):
    from src.validation import registry as rg
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    rg.ensure_schema(conn)
    monkeypatch.setattr(tod, "evidence", lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = hyp.run_nightly(conn, date(2026, 9, 23), entries=[], events=[],
                          bars_fn=lambda t, d: [], nifty_closes_fn=lambda: {})
    assert "boom" in out["tod_entry_window"]["skip"] and out["mansfield_rs"]["verdict"] == "insufficient_n"


def test_nothing_live_imports_the_hypothesis_queue():
    from pathlib import Path
    root = Path(hyp.__file__).resolve().parents[2]
    for live in ("options_proposer.py", "plan_tracker.py", "equity_desk.py", "equity_shadow_proposer.py",
                 "market_loop.py", "master_scheduler.py", "execution/paper_venue.py"):
        assert "validation.hypotheses" not in (root / live).read_text(), live
    court = (root / "validation" / "run_proving_court.py").read_text()
    assert "hyp.run_nightly(conn, today)" in court
