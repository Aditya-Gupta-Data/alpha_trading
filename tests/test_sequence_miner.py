"""
Tests for the lagged sequence miner (Phase 5, P5-2). Fully offline. Covers
the lag arithmetic, the no-look-ahead timelock contract (a new discovery
feature doesn't merge without one), entry-date anchoring + corpus split,
and one end-to-end mine → register through the shared core.

Run either of these from the project folder:
    python tests/test_sequence_miner.py
    python -m pytest tests/test_sequence_miner.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src import daily_context as dc
from src.discovery import cooccurrence_miner as cm
from src.discovery import sequence_miner as sm
from src.validation import registry as rg
from src.validation import timelock as tl


# ----------------------------------------------------- lag arithmetic

def test_lagged_tags_read_strictly_prior_frames():
    ctx_dates = ["2026-01-01", "2026-01-02", "2026-01-03",
                 "2026-01-04", "2026-01-05", "2026-01-06"]
    frames = {d: {"date": d} for d in ctx_dates}
    frames["2026-01-04"] = {"date": "2026-01-04", "fii_net": 5.0}   # index 3
    # Entry at index 5; lag2 -> index 3 (the fii-up frame), strictly before.
    tags = sm.lagged_antecedent_tags("2026-01-06", ctx_dates, frames,
                                     lags=(1, 2, 3))
    assert tags == {"lag2:ctx:fii:up"}
    # No frame at/after entry is ever consulted.
    assert not any("2026-01-06" in t for t in tags)
    # Entry before the whole series -> nothing (no fabricated antecedent).
    assert sm.lagged_antecedent_tags("2025-12-01", ctx_dates, frames) == set()


def test_no_lookahead_future_frames_never_change_antecedents():
    entry = "2026-01-06"
    base_rows = [{"date": d} for d in ["2026-01-02", "2026-01-03",
                                       "2026-01-04", "2026-01-05"]]
    base_rows[2] = {"date": "2026-01-04", "fii_net": 5.0}

    def compute(rows):
        frames = {r["date"]: r for r in rows}
        ctx_dates = sorted(frames)
        return sorted(sm.lagged_antecedent_tags(entry, ctx_dates, frames))

    # Salt with LOUD future frames (dated after entry, carrying ctx) — a
    # leak would pull them in and change the output.
    salted = tl.future_salt(base_rows, entry,
                            lambda iso: {"date": iso, "fii_net": 9.0,
                                         "dii_net": 9.0})
    tl.assert_future_blind(compute, base_rows, salted, "sequence antecedents")


# ----------------------------------------------------- transactions (DB)

def _frame(conn, day, **cols):
    dc.record_frame(conn, {"date": day, **cols})


def test_build_anchors_on_entry_date_and_splits_corpus():
    conn = brain_map.connect(":memory:")
    # Series: only 2026-01-04 carries an antecedent tag (index 2 of 4).
    for i, d in enumerate(["2026-01-02", "2026-01-03", "2026-01-04",
                           "2026-01-05", "2026-01-06"]):
        _frame(conn, d, **({"fii_net": 5.0} if d == "2026-01-04" else {}))
    # A real trade ENTERED 2026-01-06 but EXITED 2026-01-20 — anchoring on
    # exit would miss the lag; on entry it sees 2026-01-04 two frames back.
    oid = brain_map.record_outcome(
        conn, journal_ref="2026-01-06|REL|BUY|100", date="2026-01-20",
        ticker="REL", r_multiple=1.0, result="win")
    eid = brain_map.record_event(conn, "2026-01-06", "REL", "signal",
                                 "golden_cross", source="journal")
    brain_map.link_event_outcome(conn, eid, oid)
    # A sim outcome (excluded from the real corpus).
    soid = brain_map.record_outcome(
        conn, journal_ref="sim:cafe", date="2026-01-20", ticker="REL",
        r_multiple=-1.0, result="loss")
    seid = brain_map.record_event(conn, "2026-01-06", "REL", "signal",
                                  "golden_cross", source="sim")
    brain_map.link_event_outcome(conn, seid, soid)

    real = sm.build_lagged_transactions(conn, corpus="real", lags=(1, 2, 3))
    sim = sm.build_lagged_transactions(conn, corpus="sim", lags=(1, 2, 3))
    assert len(real) == 1 and len(sim) == 1
    assert real[0]["items"] == frozenset({"lag2:ctx:fii:up"})
    assert real[0]["win"] is True


# ----------------------------------------------------- end-to-end

def test_endtoend_surfaces_a_lagged_edge_and_registers_it():
    conn = brain_map.connect(":memory:")
    days = [f"2026-01-{i:02d}" for i in range(1, 13)]        # 12 frames
    for i, d in enumerate(days):
        if i == 3:          # WIN antecedent, two tags -> a size-2 itemset
            _frame(conn, d, fii_net=5.0, macro_nifty_short=0.03)
        elif i == 8:        # LOSS antecedent, two different tags
            _frame(conn, d, dii_net=5.0, deals_buy_legs=4, deals_sell_legs=1)
        else:
            _frame(conn, d)

    def add(ref_day, exit_day, ticker, win):
        oid = brain_map.record_outcome(
            conn, journal_ref=f"{ref_day}|{ticker}|BUY|100", date=exit_day,
            ticker=ticker, r_multiple=(1.0 if win else -1.0),
            result=("win" if win else "loss"))
        eid = brain_map.record_event(conn, ref_day, ticker, "signal",
                                     "sig", source="journal")
        brain_map.link_event_outcome(conn, eid, oid)

    # 15 trades entered index-5 (2026-01-06): lag2 -> the WIN antecedent.
    for i in range(15):
        add("2026-01-06", "2026-01-20", f"W{i}", win=(i < 14))
    # 13 trades entered index-10 (2026-01-11): lag2 -> the LOSS antecedent.
    for i in range(13):
        add("2026-01-11", "2026-01-25", f"L{i}", win=(i < 2))

    txns = sm.build_lagged_transactions(conn, corpus="real")
    survivors = cm.mine(txns, min_support=12, fdr_q=0.15)
    tag_sets = [tuple(s["tags"]) for s in survivors]
    assert ("lag2:ctx:fii:up", "lag2:ctx:macro_nifty:up") in tag_sets
    # The losing antecedent is significant but the WRONG way -> not a buy.
    assert ("lag2:ctx:deals:net_buy", "lag2:ctx:dii:up") not in tag_sets

    reg = sm.register_survivors(conn, survivors, "real")
    win_reg = next(r for r in reg
                   if r["tags"] == ["lag2:ctx:fii:up", "lag2:ctx:macro_nifty:up"])
    assert win_reg["created"] is True
    row = rg.get(conn, win_reg["pattern_id"])
    assert row["kind"] == "sequence" and "SEQ" in row["description"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed.")
