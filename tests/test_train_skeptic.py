"""
Phase 7b trainer tests — fully offline. Synthetic simulated_trades rows
go into an in-memory brain_map DB, the forest trains on them, and the
saved pickle must wake the Phase 11 skeptic from its abstain state.
Model files only ever land in a temp directory — never in data/.

Run from the project folder:
    python tests/test_train_skeptic.py      (simple, no extra installs)
    python -m pytest tests/                 (if you have pytest)
"""

import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src import train_skeptic as ts
from src.simulator import ensure_schema as ensure_sim_schema
from src.skeptic_agent import FEATURE_NAMES, RandomForestAuditor


def make_conn():
    conn = brain_map.connect(":memory:")
    ensure_sim_schema(conn)
    return conn


def insert_sim(conn, ref, *, vix, result, net_credit=70.0, net_debit=None,
               spread_width=400.0, max_loss=4_550.0, lots=2,
               proposed_on="2026-06-01", expiry="2026-06-11"):
    conn.execute(
        "INSERT INTO simulated_trades (journal_ref, underlying, strategy, "
        "proposed_on, expiry, vix, net_credit, net_debit, spread_width, "
        "max_loss, max_profit, lots, lot_size, resolution, exit_date, "
        "pnl_net, frictions_rs, slippage_rs, capture_pct, r_multiple, "
        "result, verdict) VALUES (?, 'NIFTY 50', 'iron_condor', ?, ?, ?, "
        "?, ?, ?, ?, 2450.0, ?, 75, 'profit_take', ?, "
        "?, 100.0, 50.0, 65.0, 0.5, ?, 'test')",
        (ref, proposed_on, expiry, vix, net_credit, net_debit, spread_width,
         max_loss, lots, expiry, 1000.0 if result == "win" else -2000.0,
         result))
    conn.commit()


def seed_separable(conn, wins=25, losses=15):
    """A pattern the forest must find: calm VIX wins, panic VIX loses."""
    for i in range(wins):
        insert_sim(conn, f"win{i}", vix=11.0 + (i % 5) * 0.5, result="win")
    for i in range(losses):
        insert_sim(conn, f"loss{i}", vix=24.0 + (i % 5) * 0.5, result="loss")


# --- the feature contract --------------------------------------------------

def test_row_features_honor_the_frozen_feature_order():
    row = {"vix": 14.5, "net_credit": 70.0, "net_debit": None,
           "spread_width": 400.0, "max_loss": 4_550.0, "lots": 3,
           "proposed_on": "2026-06-01", "expiry": "2026-06-11"}
    vec = ts.row_features(row)
    assert len(vec) == len(FEATURE_NAMES)
    assert vec[:4] == [0.0, 0.0, 0.0, 0.0]     # graph slots: honestly zero
    assert vec[FEATURE_NAMES.index("vix")] == 14.5
    assert vec[FEATURE_NAMES.index("net_credit_or_debit")] == 70.0
    assert vec[FEATURE_NAMES.index("spread_width")] == 400.0
    assert vec[FEATURE_NAMES.index("days_to_expiry")] == 10.0
    assert vec[FEATURE_NAMES.index("max_loss_per_lot")] == 4_550.0
    assert vec[FEATURE_NAMES.index("lots")] == 3.0


def test_debit_structures_encode_as_negative_net():
    row = {"vix": 12.0, "net_credit": None, "net_debit": 55.0,
           "spread_width": 400.0, "max_loss": 4_125.0, "lots": 1,
           "proposed_on": "2026-06-01", "expiry": "2026-06-08"}
    vec = ts.row_features(row)
    assert vec[FEATURE_NAMES.index("net_credit_or_debit")] == -55.0


def test_scratch_rows_are_dropped_from_training():
    conn = make_conn()
    insert_sim(conn, "w1", vix=12.0, result="win")
    insert_sim(conn, "s1", vix=12.0, result="scratch")
    insert_sim(conn, "l1", vix=25.0, result="loss")
    X, y = ts.features_and_labels(ts.load_training_rows(conn))
    assert len(X) == len(y) == 2
    assert sorted(y) == [0, 1]


# --- refusal guards ---------------------------------------------------------

def test_refuses_to_train_on_too_few_rows():
    conn = make_conn()
    seed_separable(conn, wins=10, losses=10)   # 20 < MIN_TRAINING_ROWS
    try:
        ts.run_training(conn=conn, dry_run=True)
        assert False, "should have refused"
    except ValueError as e:
        assert "longer simulation" in str(e)


def test_refuses_a_starved_minority_class():
    conn = make_conn()
    seed_separable(conn, wins=40, losses=3)    # 43 rows but 3 losses
    try:
        ts.run_training(conn=conn, dry_run=True)
        assert False, "should have refused"
    except ValueError as e:
        assert "class too thin" in str(e)


# --- training end-to-end ----------------------------------------------------

def test_training_learns_the_separable_pattern_and_saves_artifacts():
    conn = make_conn()
    seed_separable(conn)
    with tempfile.TemporaryDirectory() as tmp:
        model_path = Path(tmp) / "skeptic_model.pkl"
        metrics = ts.run_training(conn=conn, model_path=model_path)
        assert metrics["rows"] == 40
        assert metrics["holdout_balanced_accuracy"] >= 0.9   # trivially separable
        assert model_path.exists()
        meta_path = Path(tmp) / "skeptic_model_meta.json"
        assert meta_path.exists()
        meta = __import__("json").loads(meta_path.read_text())
        assert meta["feature_names"] == list(FEATURE_NAMES)
        assert meta["wins"] == 25 and meta["losses"] == 15


def seed_unseparable(conn, wins=20, losses=20):
    """Identical features, mixed labels — no honest model exists here."""
    for i in range(wins):
        insert_sim(conn, f"uw{i}", vix=14.0, result="win")
    for i in range(losses):
        insert_sim(conn, f"ul{i}", vix=14.0, result="loss")


def test_ship_gate_refuses_a_coin_flip_model():
    conn = make_conn()
    seed_unseparable(conn)
    with tempfile.TemporaryDirectory() as tmp:
        model_path = Path(tmp) / "skeptic_model.pkl"
        metrics = ts.run_training(conn=conn, model_path=model_path)
        assert metrics["shippable"] is False
        assert metrics["saved"] is False
        assert not model_path.exists()   # the skeptic keeps abstaining


def test_force_overrides_the_ship_gate_for_experiments():
    conn = make_conn()
    seed_unseparable(conn)
    with tempfile.TemporaryDirectory() as tmp:
        model_path = Path(tmp) / "skeptic_model.pkl"
        metrics = ts.run_training(conn=conn, model_path=model_path, force=True)
        assert metrics["saved"] is True and model_path.exists()


def test_dry_run_writes_nothing():
    conn = make_conn()
    seed_separable(conn)
    with tempfile.TemporaryDirectory() as tmp:
        model_path = Path(tmp) / "skeptic_model.pkl"
        ts.run_training(conn=conn, model_path=model_path, dry_run=True)
        assert not model_path.exists()
        assert list(Path(tmp).iterdir()) == []


def test_metrics_are_deterministic_across_runs():
    conn = make_conn()
    seed_separable(conn)
    a = ts.run_training(conn=conn, dry_run=True)
    b = ts.run_training(conn=conn, dry_run=True)
    assert a == b


# --- the trained pickle must wake the skeptic --------------------------------

def _proposal(vix: float) -> dict:
    expiry = (date.today() + timedelta(days=10)).isoformat()
    return {"vix": vix, "lots": 2,
            "spread": {"net_credit": 70.0, "net_debit": None,
                       "spread_width": 400.0, "max_loss": 4_550.0,
                       "expiry": expiry}}


def test_saved_model_flips_the_skeptic_from_abstain_to_live():
    conn = make_conn()
    seed_separable(conn)
    with tempfile.TemporaryDirectory() as tmp:
        model_path = Path(tmp) / "skeptic_model.pkl"
        ts.run_training(conn=conn, model_path=model_path)

        auditor = RandomForestAuditor(model_path=model_path)
        calm = auditor.audit(_proposal(vix=11.5))
        panic = auditor.audit(_proposal(vix=25.5))
        # live probabilities, not abstention
        assert calm["probability"] is not None
        assert panic["probability"] is not None
        # ...and they encode the trained pattern: calm condors look good,
        # panic condors trip the sub-0.40 warning
        assert calm["probability"] > 0.6 and calm["warn"] is False
        assert panic["probability"] < 0.4 and panic["warn"] is True


def test_without_a_model_file_the_skeptic_still_abstains():
    with tempfile.TemporaryDirectory() as tmp:
        auditor = RandomForestAuditor(model_path=Path(tmp) / "missing.pkl")
        result = auditor.audit(_proposal(vix=25.5))
        assert result["probability"] is None and result["warn"] is False


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
