"""Earnings Awareness S1-S3 (decision #150, docs/earnings_awareness_spec.md). Fully offline."""
import json
from datetime import date, timedelta

import pytest

from src import brain_map, lake
from src.confluence import evidence as ev
from src.ingestion import earnings_calendar as ec

TODAY = date(2026, 10, 16)
HEAVY = {"NIFTY BANK": ["HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS"]}


def _doc(events=None, last=None, as_of=TODAY, source="nse"):
    return {"as_of": as_of.isoformat(), "source": source,
            "events": events if events is not None else {"ZZZ.NS": "2027-01-01"},
            "past": {}, "last": last if last is not None else {}}


def _iso(days):
    return (TODAY + timedelta(days=days)).isoformat()


# ------------------------------------------------------------------ S1

def _copy(root, day, events, past=None):
    row = {"as_of": day, "source": "nse", "events": events}
    if past is not None:
        row["past"] = past
    lake.write_partition("earnings", day, [row], root=root)


def test_last_survives_a_moved_date_and_the_ticker_dropping_out(tmp_path):
    root = tmp_path / "lake"
    _copy(root, "2026-07-10", {"TCS.NS": "2026-07-14", "INFY.NS": "2026-07-16"})
    _copy(root, "2026-07-11", {"TCS.NS": "2026-07-18", "INFY.NS": "2026-07-16"})   # TCS postponed
    _copy(root, "2026-07-15", {"TCS.NS": "2026-07-18", "INFY.NS": "2026-07-16"})
    _copy(root, "2026-07-17", {"TCS.NS": "2026-07-18"})                            # INFY reported, dropped out
    _copy(root, "2026-07-19", {"TCS.NS": "2026-10-20"})                            # next quarter
    last = ec.rebuild_last(root, today=date(2026, 7, 20))
    assert last == {"INFY.NS": "2026-07-16", "TCS.NS": "2026-07-18"}               # 07-14 never happened
    assert ec.rebuild_last(root, today=date(2026, 7, 15)) == {}
    doc = {"as_of": "2026-07-19", "source": "nse", "events": {"TCS.NS": "2026-10-20"},
           "past": {}, "last": last}
    assert ec.next_results("TCS.NS", date(2026, 7, 20), doc) == "2026-10-20"
    assert ec.days_since_results("TCS.NS", date(2026, 7, 20), doc) == 2
    assert ec.days_since_results("INFY.NS", date(2026, 7, 20), doc) == 4
    assert ec.days_since_results("WIPRO.NS", date(2026, 7, 20), doc) is None


def test_run_keeps_last_beside_next_and_a_past_date_never_masks_the_next(tmp_path, monkeypatch):
    out, root = tmp_path / "cal.json", tmp_path / "lake"
    day1 = [{"symbol": "TCS", "purpose": "Financial Results", "date": "14-Jul-2026"}]
    day2 = day1 + [{"symbol": "TCS", "purpose": "Financial Results", "date": "20-Oct-2026"}]
    for rows, today in ((day1, date(2026, 7, 10)), (day2, date(2026, 7, 15))):
        monkeypatch.setattr(ec, "_fetch_nse_calendar",
                            lambda *a, rows=rows, **k: (rows, json.dumps(rows).encode()))
        cal = ec.run(output_path=out, snapshot_path=tmp_path / "none.json",
                     lake_root=root, today=today)
    assert cal["events"] == {"TCS.NS": "2026-10-20"}
    assert cal["past"] == {"TCS.NS": "2026-07-14"} and cal["last"] == {"TCS.NS": "2026-07-14"}
    doc = ec.load_doc(out)
    assert doc["last"] == {"TCS.NS": "2026-07-14"}
    assert ec.days_since_results("TCS.NS", date(2026, 7, 16), doc) == 2
    assert ec.days_to_results("TCS.NS", date(2026, 7, 16), doc["events"]) == 96


@pytest.mark.parametrize("doc, why", [
    ({}, "missing"),
    (_doc(as_of=TODAY - timedelta(days=4)), "stale"),
    (_doc(as_of=TODAY + timedelta(days=1)), "after"),
    (_doc(events={}, source="none"), "empty"),
    ({"as_of": TODAY.isoformat(), "source": "nse", "events": {"TCS.NS": _iso(3)}}, "past-results memory"),
])
def test_a_stale_or_missing_calendar_reads_none_with_a_reason(doc, why):
    ctx = ec.results_context("TCS.NS", TODAY, doc)
    assert why in ctx["reason"]
    assert ctx["next_results"] is None and ctx["days_to_results"] is None
    assert ctx["last_results"] is None and ctx["days_since_results"] is None


def test_three_day_old_calendar_still_speaks():
    ctx = ec.results_context("TCS.NS", TODAY, _doc(events={"TCS.NS": _iso(1)},
                                                   as_of=TODAY - timedelta(days=3)))
    assert ctx["reason"] is None and ctx["days_to_results"] == 1


def test_index_constituent_results_reads_the_window_from_the_map():
    doc = _doc(events={"HDFCBANK.NS": _iso(2), "ICICIBANK.NS": _iso(9)},
               last={"SBIN.NS": _iso(-6)})
    hits = ec.index_constituent_results("nifty bank", TODAY, _iso(11), doc, HEAVY)
    assert hits == [{"ticker": "HDFCBANK.NS", "results_date": _iso(2)},
                    {"ticker": "ICICIBANK.NS", "results_date": _iso(9)}]
    wider = ec.index_constituent_results("NIFTY BANK", _iso(-10), _iso(11), doc, HEAVY)
    assert [h["ticker"] for h in wider] == ["SBIN.NS", "HDFCBANK.NS", "ICICIBANK.NS"]
    assert ec.index_constituent_results("NIFTY 50", TODAY, _iso(11), doc, HEAVY) is None


def test_shipped_heavyweight_map_is_data_for_the_index_underlyings():
    heavy = ec.load_heavyweights()
    assert set(heavy) == {"NIFTY 50", "NIFTY BANK", "NIFTY FIN SERVICE", "NIFTY MID SELECT", "NIFTY IT"}
    assert heavy["NIFTY IT"][:2] == ["TCS.NS", "INFY.NS"]             # #151: TCS and INFY move NIFTY IT
    assert all(t.endswith(".NS") for names in heavy.values() for t in names)
    assert all(len(names) <= 5 for names in heavy.values())


def test_unannounced_next_results_cannot_fall_before_the_next_quarter_end():
    reported = _doc(last={"TCS.NS": "2026-10-09"})
    assert ec.earliest_unannounced_results("TCS.NS", TODAY, reported) == "2027-01-01"
    pending = _doc(last={"TCS.NS": "2026-07-18"})                  # Sep quarter not reported yet
    assert ec.earliest_unannounced_results("TCS.NS", TODAY, pending) is None
    assert ec.earliest_unannounced_results("TCS.NS", TODAY, _doc()) is None


# ------------------------------------------------------------------ S2

def _block(ticker, expiry_days, doc, heavy=HEAVY):
    return ev.earnings_block(ticker, _iso(expiry_days), TODAY, doc, heavy)


def test_entry_two_days_before_results_is_pre():
    b = _block("HDFCBANK.NS", 11, _doc(events={"HDFCBANK.NS": _iso(2)},
                                       last={"HDFCBANK.NS": "2026-07-19"}))
    assert b["window"] == "pre" and b["results_before_expiry"] is True
    assert b["days_to_results"] == 2 and b["next_results"] == _iso(2)
    assert b["last_results"] == "2026-07-19" and b["days_since_results"] == 89
    assert b["basis"] == "own" and b["index_constituents_reporting"] == []
    assert b["source_as_of"] == TODAY.isoformat() and b["reason"] is None


def test_through_post_and_clear_windows():
    through = _block("INFY.NS", 20, _doc(events={"INFY.NS": _iso(10)}))
    assert through["window"] == "through" and through["results_before_expiry"] is True
    after_expiry = _block("INFY.NS", 5, _doc(events={"INFY.NS": _iso(6)}))
    assert after_expiry["results_before_expiry"] is False and after_expiry["window"] == "clear"
    post = _block("TCS.NS", 11, _doc(last={"TCS.NS": _iso(-2)}))
    assert post["window"] == "post" and post["results_before_expiry"] is False
    clear = _block("TCS.NS", 11, _doc(last={"TCS.NS": _iso(-7)}))
    assert clear["window"] == "clear" and clear["days_since_results"] == 7


def test_unannounced_results_are_labelled_not_announced_never_clear():
    b = _block("RELIANCE.NS", 11, _doc(last={"RELIANCE.NS": "2026-07-18"}))
    assert b["window"] == "not_announced" and b["results_before_expiry"] is None   # #151
    assert "not announced for RELIANCE.NS" in b["reason"]


def test_index_reads_its_heavyweights():
    doc = _doc(events={"HDFCBANK.NS": _iso(2), "ICICIBANK.NS": _iso(30)},
               last={"SBIN.NS": "2026-10-10", "HDFCBANK.NS": "2026-07-19",
                     "ICICIBANK.NS": "2026-07-26"})
    b = _block("NIFTY BANK", 11, doc)
    assert b["basis"] == "index_heavyweights" and b["window"] == "pre"
    assert b["index_constituents_reporting"] == [{"ticker": "HDFCBANK.NS",
                                                  "results_date": _iso(2)}]
    unmapped = _block("NIFTY MID SELECT", 11, doc, heavy={"NIFTY MID SELECT": []})
    assert unmapped["window"] is None and "no heavyweight map" in unmapped["reason"]


def test_missing_calendar_stamps_window_null_with_reason():
    entry = {"date": TODAY.isoformat(), "ticker": "TCS.NS", "spread": {"expiry": _iso(11)}}
    block = ev.stamp_earnings(entry, doc={})
    assert entry["earnings"] is block
    assert block["window"] is None and block["reason"] == "earnings calendar missing"


def test_a_broken_stamp_still_leaves_a_reasoned_block(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("calendar exploded")
    monkeypatch.setattr(ev, "earnings_block", boom)
    entry = {"date": TODAY.isoformat(), "ticker": "TCS.NS", "spread": {}}
    assert ev.stamp_earnings(entry)["window"] is None
    assert "calendar exploded" in entry["earnings"]["reason"]


def test_the_entry_is_never_refused_or_resized_by_the_block(tmp_path, monkeypatch):
    from src import journal as journal_mod
    from src import options_proposer as op
    today = date.today()
    expiry = (today + timedelta(days=20)).isoformat()
    heavy = tmp_path / "heavy.json"
    heavy.write_text(json.dumps({"indices": {"NIFTY 50": ["HDFCBANK.NS"]}}))
    monkeypatch.setattr(ec, "HEAVYWEIGHTS_PATH", heavy)
    strikes = {f"{k}.000000": {"ce": {"last_price": max(5.0, (25000 - k) * 0.4 + 120)},
                                "pe": {"last_price": max(5.0, (k - 25000) * 0.4 + 120)}}
               for k in range(23000, 27050, 50)}
    state = {"analysis": {"ticker": "NIFTY 50", "uptrend": False, "fresh_cross": False,
                          "rsi": 52.0, "price": 25000.0},
             "vix": 14.5, "chain": {"last_price": 25000.0, "oc": strikes},
             "expiry": expiry, "book": {"cash": 1_000_000.0, "holdings": {}}}
    pre = {"as_of": today.isoformat(), "source": "nse", "past": {}, "last": {},
           "events": {"HDFCBANK.NS": (today + timedelta(days=1)).isoformat()}}
    results = {}
    for name, doc in (("pre", pre), ("missing", None)):
        cal = tmp_path / f"{name}.json"
        if doc is not None:
            cal.write_text(json.dumps(doc))
        monkeypatch.setattr(ec, "OUTPUT_PATH", cal)
        monkeypatch.setattr(journal_mod, "JOURNAL_PATH", tmp_path / f"{name}.jsonl")
        results[name] = op.run_headless("NIFTY 50", state=dict(state))
    a, b = results["pre"], results["missing"]
    assert a["proposed"] == b["proposed"] and a["reason"] == b["reason"]
    if a["proposed"]:
        assert a["entry"]["earnings"]["window"] == "pre"
        assert b["entry"]["earnings"]["window"] is None
        for key in ("decision", "lots"):
            assert a["entry"].get(key) == b["entry"].get(key)
        assert a["entry"]["spread"] == b["entry"]["spread"]


# ------------------------------------------------------------------ S3

def _settled_entry(window="pre", before=True):
    return {
        "short_id": "earn0001", "date": "2026-10-14", "action": "SELL",
        "ticker": "HDFCBANK.NS", "shares": 1, "price": 100.0, "view": "neutral",
        "signal": "iron butterfly", "decision": "approved", "why": "test",
        "pattern_tags": ["iron_butterfly"],
        "spread": {"strategy": "iron_butterfly", "expiry": "2026-10-27"},
        "earnings": {"window": window, "results_before_expiry": before,
                     "reason": None},
        "outcome": {"resolution": "expiry", "price": 90.0, "exit_date": "2026-10-27",
                    "pct": -10.0, "r_multiple": -0.8, "days_in_trade": 13,
                    "pnl_rs": -800.0, "hypothetical": False, "verdict": "LOSS"},
    }


def test_settlement_copies_the_window_onto_outcome_court_and_shadow_rows(monkeypatch):
    from src import analyst, plan_tracker, shadow_learner
    from src.validation import trial
    conn = brain_map.connect(":memory:")
    entry = _settled_entry()
    ref = brain_map.journal_ref_for(entry)
    court = shadow_learner.record_fire(conn, entry, fire_date="2026-10-14")
    pattern = trial.record_shadow_fire(conn, "pat:x", "2026-10-14", "HDFCBANK.NS")
    conn.execute("UPDATE shadow_trades SET host_ref = ? WHERE journal_ref = ?",
                 (ref, pattern["ref"]))
    blocked = trial.record_block(conn, "exposure", "2026-10-15", "HDFCBANK.NS",
                                 direction="neutral", host_ref=ref)
    kept = trial.record_signal_fire(conn, "h4", "2026-10-15", "HDFCBANK.NS", host_ref=ref)
    conn.execute("UPDATE shadow_trades SET earnings_window = 'clear', results_before_expiry = 0 "
                 "WHERE journal_ref = ?", (kept["ref"],))
    stranger = trial.record_shadow_fire(conn, "pat:y", "2026-10-14", "TCS.NS")
    conn.commit()
    row = conn.execute("SELECT earnings_window, results_before_expiry FROM shadow_trades "
                       "WHERE journal_ref = ?", (court["ref"],)).fetchone()
    assert tuple(row) == ("pre", 1)                             # the Court row is born labelled
    monkeypatch.setattr(analyst, "generate_post_mortem", lambda p, e: None)
    plan_tracker.record_post_mortem(entry, conn)
    out = conn.execute("SELECT earnings_window, results_before_expiry FROM outcomes "
                       "WHERE journal_ref = ?", (ref,)).fetchone()
    assert tuple(out) == ("pre", 1)
    labels = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT journal_ref, earnings_window, results_before_expiry FROM shadow_trades")}
    assert labels[pattern["ref"]] == ("pre", 1)
    assert labels[blocked["ref"]] == ("pre", 1)
    assert labels[kept["ref"]] == ("clear", 0)                  # an existing label is kept
    assert labels[stranger["ref"]] == (None, None)              # other trades untouched


def test_unstamped_entries_settle_exactly_as_before(monkeypatch):
    from src import analyst, plan_tracker
    conn = brain_map.connect(":memory:")
    entry = _settled_entry()
    del entry["earnings"]
    monkeypatch.setattr(analyst, "generate_post_mortem", lambda p, e: None)
    plan_tracker.record_post_mortem(entry, conn)
    out = conn.execute("SELECT result, earnings_window, results_before_expiry FROM outcomes"
                       ).fetchone()
    assert tuple(out) == ("loss", None, None)


def test_strategy_stats_splits_outcomes_by_earnings_window(tmp_path):
    from src import brain_mcp
    db = tmp_path / "data" / "brain_map.db"
    conn = brain_map.connect(db)
    rows = [("a1", "iron_condor", 0.5, "pre", True), ("a2", "iron_condor", -1.0, "pre", True),
            ("a3", "iron_condor", 0.4, "clear", False), ("a4", "bear_put_spread", 1.0, None, None),
            ("placebo:z", "iron_condor", 2.0, "pre", True)]
    for ref, arch, r, window, before in rows:
        brain_map.record_outcome(conn, ref, "2026-10-14", "HDFCBANK.NS", archetype=arch,
                                 r_multiple=r, earnings_window=window,
                                 results_before_expiry=before)
    conn.close()
    stats = brain_mcp.strategy_stats(brain_mcp.Sources(root=tmp_path, db_path=db), {})
    cells = {(c["window"], c["strategy"]): c for c in stats["by_earnings_window"]}
    assert set(cells) == {("pre", "iron_condor"), ("clear", "iron_condor"),
                          ("unlabelled", "bear_put_spread")}
    pre = cells[("pre", "iron_condor")]
    assert (pre["n"], pre["wins"], pre["results_before_expiry"]) == (2, 1, 2)
    assert pre["readable"] is False
    assert "n >= 20" in stats["earnings_basis"]


# ------------------------------------------------------------- #151 exit label

def test_the_exit_label_says_whether_the_hold_crossed_results():
    doc = _doc(events={"INFY.NS": _iso(30)}, last={"TCS.NS": "2026-10-09", "INFY.NS": "2026-07-16"})
    assert ev.results_crossed("TCS.NS", "2026-10-01", "2026-10-16", TODAY, doc) is True
    assert ev.results_crossed("TCS.NS", "2026-10-10", "2026-10-16", TODAY, doc) is False
    assert ev.results_crossed("INFY.NS", "2026-10-01", _iso(30), TODAY, doc) is True       # results on the exit day
    assert ev.results_crossed("NIFTY IT", "2026-10-01", "2026-10-16", TODAY, doc,
                              heavyweights={"NIFTY IT": ["TCS.NS", "INFY.NS"]}) is True
    assert ev.results_crossed("NIFTY MID SELECT", "2026-10-01", "2026-10-16", TODAY, doc,
                              heavyweights={"NIFTY MID SELECT": []}) is None
    assert ev.results_crossed("TCS.NS", "2026-10-01", "2026-10-16", TODAY, {}) is None     # calendar missing
    assert ev.results_crossed("TCS.NS", None, "2026-10-16", TODAY, doc) is None


def test_settlement_labels_entry_and_exit(monkeypatch, tmp_path):
    from src import analyst, plan_tracker, shadow_learner
    cal = tmp_path / "cal.json"
    cal.write_text(json.dumps(_doc(last={"HDFCBANK.NS": "2026-10-17"}, as_of=date.today())))
    monkeypatch.setattr(ec, "OUTPUT_PATH", cal)
    monkeypatch.setattr(analyst, "generate_post_mortem", lambda p, e: None)
    conn = brain_map.connect(":memory:")
    entry = _settled_entry()
    court = shadow_learner.record_fire(conn, entry, fire_date="2026-10-14")
    plan_tracker.record_post_mortem(entry, conn)
    ref = brain_map.journal_ref_for(entry)
    out = conn.execute("SELECT earnings_window, results_crossed FROM outcomes WHERE journal_ref = ?",
                       (ref,)).fetchone()
    assert tuple(out) == ("pre", 1)                     # known at entry: pre; what happened: crossed
    row = conn.execute("SELECT earnings_window, results_crossed FROM shadow_trades WHERE journal_ref = ?",
                       (court["ref"],)).fetchone()
    assert tuple(row) == ("pre", 1)
