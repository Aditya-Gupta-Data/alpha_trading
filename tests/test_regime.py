"""
Regime-Aware Memory tests — fully offline: in-memory DBs, synthetic
closes, no network. Covers the pure vocabulary, capture at entry
creation (live + simulated paths), the outcomes/simulated_trades schema
migrations, the regime-filtered Brain Map query, the skeptic's new
regime features, and the historical backfill.

Run from the project folder:
    python tests/test_regime.py      (simple, no extra installs)
    python -m pytest tests/          (if you have pytest)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src import regime as rg
from src.simulator import ensure_schema as ensure_sim_schema


# --- the vocabulary ---------------------------------------------------------

def test_vix_band_boundaries():
    assert rg.vix_band(None) == "unknown"
    assert rg.vix_band("garbage") == "unknown"
    assert rg.vix_band(12.99) == "low"
    assert rg.vix_band(13.0) == "mid"
    assert rg.vix_band(15.99) == "mid"
    assert rg.vix_band(16.0) == "high"
    assert rg.vix_band(35.0) == "high"


def test_regime_for_and_tag():
    r = rg.regime_for("bearish", 14.5)
    assert r == {"trend": "bearish", "vix_band": "mid", "vix": 14.5}
    assert rg.regime_tag(r) == "bearish+mid_iv"
    assert rg.regime_for("nonsense", None) == \
        {"trend": "unknown", "vix_band": "unknown", "vix": None}
    assert rg.regime_tag({}) == "unknown+unknown_iv"


def test_encode_for_model():
    assert rg.encode_for_model("bullish", "low") == (1.0, 0.0)
    assert rg.encode_for_model("neutral", "mid") == (0.0, 1.0)
    assert rg.encode_for_model("bearish", "high") == (-1.0, 2.0)
    assert rg.encode_for_model("unknown", "unknown") == (0.0, -1.0)
    assert rg.encode_for_model(None, None) == (0.0, -1.0)


# --- capture at creation ------------------------------------------------------

def _proposal(view="neutral", vix=14.0):
    return {"action": "SPREAD", "ticker": "NIFTY BANK", "shares": 70,
            "price": 120.0, "signal": "test", "view": view, "vix": vix,
            "lots": 2,
            "spread": {"strategy": "iron_condor", "lot_size": 35, "lots": 2,
                       "expiry": "2026-07-21", "legs": []}}


def test_to_journal_entry_carries_the_regime():
    from src.options_proposer import to_journal_entry
    entry = to_journal_entry(_proposal(view="bearish", vix=16.2),
                             "pending_approval", "why")
    assert entry["regime"] == {"trend": "bearish", "vix_band": "high",
                               "vix": 16.2}


def test_simulator_entry_carries_the_regime():
    from src.simulator import _entry_for
    entry = _entry_for(_proposal(view="neutral", vix=12.1), "2026-06-01",
                       "sim:abc")
    assert entry["regime"] == {"trend": "neutral", "vix_band": "low",
                               "vix": 12.1}
    assert entry["short_id"] == "sim:abc"      # nothing else disturbed
    assert entry["decision"] == "approved"


# --- schema migrations + outcome write ------------------------------------------

def test_outcomes_migration_and_regime_write():
    conn = brain_map.connect(":memory:")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(outcomes)")}
    assert {"regime_trend", "regime_vix"} <= cols
    entry = {"short_id": "reg00001", "date": "2026-07-01",
             "ticker": "NIFTY BANK", "signal": "iron_condor setup",
             "pattern_tags": ["iron_condor"],
             "spread": {"strategy": "iron_condor"},
             "regime": {"trend": "neutral", "vix_band": "mid", "vix": 14.0},
             "outcome": {"exit_date": "2026-07-08", "r_multiple": 1.2}}
    brain_map.record_resolved_entry(conn, entry)
    row = conn.execute("SELECT regime_trend, regime_vix FROM outcomes "
                       "WHERE journal_ref = 'reg00001'").fetchone()
    assert (row["regime_trend"], row["regime_vix"]) == ("neutral", "mid")


def test_pre_regime_entries_record_null_tags():
    conn = brain_map.connect(":memory:")
    entry = {"short_id": "old00001", "date": "2026-07-01",
             "ticker": "TCS.NS", "signal": "golden cross",
             "outcome": {"exit_date": "2026-07-05", "r_multiple": -0.5}}
    brain_map.record_resolved_entry(conn, entry)     # no regime key at all
    row = conn.execute("SELECT regime_trend, regime_vix FROM outcomes").fetchone()
    assert row["regime_trend"] is None and row["regime_vix"] is None


# --- the regime-filtered memory query --------------------------------------------

def _seed_outcomes(conn):
    for i, (result_r, trend, band) in enumerate((
            (2.0, "neutral", "mid"), (1.5, "neutral", "mid"),
            (-1.0, "neutral", "high"), (-1.0, "bearish", "mid"),
            (1.0, None, None))):                       # a pre-regime row
        entry = {"short_id": f"q{i}", "date": f"2026-06-0{i+1}",
                 "ticker": "NIFTY BANK", "signal": "iron_condor setup",
                 "pattern_tags": ["iron_condor"],
                 "spread": {"strategy": "iron_condor"},
                 "outcome": {"exit_date": f"2026-06-1{i}",
                             "r_multiple": result_r}}
        if trend:
            entry["regime"] = {"trend": trend, "vix_band": band}
        brain_map.record_resolved_entry(conn, entry)


def test_query_similar_events_regime_filter_and_backward_compat():
    conn = brain_map.connect(":memory:")
    _seed_outcomes(conn)
    # backward compatible: no regime argument -> identical old shape
    overall = brain_map.query_similar_events(conn, ["iron_condor"])
    assert overall["count"] == 5 and "in_regime" not in overall

    filtered = brain_map.query_similar_events(
        conn, ["iron_condor"], regime={"trend": "neutral", "vix_band": "mid"})
    assert filtered["count"] == 5                     # overall untouched
    assert filtered["in_regime"]["count"] == 2        # only the 2 matches
    assert filtered["in_regime"]["win_rate"] == 1.0   # both were wins
    assert filtered["in_regime"]["tag"] == "neutral+mid_iv"
    # the pre-regime NULL row never silently matches a filter
    high = brain_map.query_similar_events(
        conn, ["iron_condor"], regime={"trend": "neutral", "vix_band": "high"})
    assert high["in_regime"]["count"] == 1 and high["in_regime"]["win_rate"] == 0.0


# --- the skeptic's regime features ------------------------------------------------

def test_skeptic_features_carry_the_regime_slots():
    from src.skeptic_agent import FEATURE_NAMES, RandomForestAuditor
    assert FEATURE_NAMES[-2:] == ("regime_trend", "regime_vix_band")
    auditor = RandomForestAuditor(model_path="/nonexistent.pkl")
    feats = auditor.generate_features(_proposal(view="bearish", vix=17.0))
    assert feats[FEATURE_NAMES.index("regime_trend")] == -1.0
    assert feats[FEATURE_NAMES.index("regime_vix_band")] == 2.0


def test_trainer_row_features_use_stored_tags_with_view_fallback():
    from src.train_skeptic import row_features
    from src.skeptic_agent import FEATURE_NAMES
    base = {"vix": 14.0, "net_credit": 70.0, "net_debit": None,
            "spread_width": 400.0, "max_loss": 4550.0, "lots": 1,
            "proposed_on": "2026-06-01", "expiry": "2026-06-11"}
    tagged = row_features(dict(base, regime_trend="bearish",
                               regime_vix="high", view="neutral"))
    assert tagged[FEATURE_NAMES.index("regime_trend")] == -1.0   # tag wins
    assert tagged[FEATURE_NAMES.index("regime_vix_band")] == 2.0
    fallback = row_features(dict(base, view="bullish"))          # no tags
    assert fallback[FEATURE_NAMES.index("regime_trend")] == 1.0  # via view
    assert fallback[FEATURE_NAMES.index("regime_vix_band")] == 1.0  # via vix


# --- historical backfill -------------------------------------------------------

def _insert_sim(conn, ref, day, vix, underlying="NIFTY 50"):
    conn.execute(
        "INSERT INTO simulated_trades (journal_ref, underlying, strategy, "
        "proposed_on, expiry, vix, resolution, exit_date, pnl_net, "
        "frictions_rs, slippage_rs, result) VALUES (?, ?, 'iron_condor', "
        "?, '2026-06-30', ?, 'profit_take', '2026-06-25', 100.0, 1.0, "
        "1.0, 'win')", (ref, underlying, day, vix))
    conn.commit()


def test_backfill_tags_untagged_rows_as_of_their_date():
    conn = brain_map.connect(":memory:")
    ensure_sim_schema(conn)
    _insert_sim(conn, "bf1", "2026-06-20", 14.5)
    _insert_sim(conn, "bf2", "2026-06-20", None)
    # synthetic rising bars: 250 sessions ending past the trade dates —
    # a clean uptrend, whose market_view read is deterministic
    bars = [(f"2026-{3 + i // 100:02d}-{(i % 28) + 1:02d}", 0, 0,
             20_000 + i * 10) for i in range(250)]
    # give them strictly increasing ISO dates instead of the messy synth
    bars = [(f"2025-{(i // 28) % 12 + 1:02d}-{i % 28 + 1:02d}", 0, 0,
             20_000 + i * 10) for i in range(250)]
    stats = rg.backfill_simulated_trades(conn, {"NIFTY 50": bars})
    assert stats["examined"] == 2 and stats["tagged"] == 2
    rows = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT journal_ref, regime_trend, regime_vix FROM simulated_trades")}
    trend, band = rows["bf1"]
    assert trend in ("bullish", "neutral", "bearish")   # a real read, not NULL
    assert band == "mid"
    assert rows["bf2"][1] == "unknown"                  # NULL vix -> unknown band
    # idempotent: second run finds nothing left to tag
    stats = rg.backfill_simulated_trades(conn, {"NIFTY 50": bars})
    assert stats["examined"] == 0


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError:
            print(f"FAIL  {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed.")
