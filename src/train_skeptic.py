"""
Alpha Trading — Phase 7b: training the Random Forest Skeptic
=============================================================

Fits the Phase 11 skeptic's Random Forest on the Phase 7 simulator's
resolved trades (`simulated_trades` in brain_map.db) and persists it to
`data/skeptic_model.pkl` — the file whose mere existence flips
`skeptic_agent.RandomForestAuditor` from ABSTAIN to live P(win) audits
in every proposal alert.

The feature matrix is built in EXACTLY `skeptic_agent.FEATURE_NAMES`
order (the frozen contract — a different order silently scores
garbage):

  * the four graph_* features are ZERO for simulated rows — honestly so:
    the simulator drives build_proposal directly, which never consults
    the knowledge graph, so at proposal time there was no graph context.
    Baking zeros in (rather than backfilling from today's graph) avoids
    look-ahead leakage; live audits will feed real graph numbers into
    the same slots.
  * the market features come straight off the row: vix,
    net_credit − net_debit, spread_width, (expiry − proposed_on) days,
    max_loss (per lot), lots.

Label: result — 'win' -> 1, 'loss' -> 0; 'scratch' rows are dropped
(a flat outcome teaches neither class). Class imbalance (condors win
often, until they don't) is handled with class_weight="balanced".

Evaluation before persisting: a stratified holdout (25%) reports
accuracy, balanced accuracy, and the confusion matrix; the model
shipped to disk is then refit on ALL rows (with this little data,
every loss row is precious). A metadata sidecar
(`data/skeptic_model_meta.json`) records when/what/how-well, so a
future session can tell a stale model at a glance.

Run from the project folder:

    python3 -m src.train_skeptic              # train + save
    python3 -m src.train_skeptic --dry-run    # evaluate only, save nothing
"""

import argparse
import json
import pickle
from datetime import date, datetime

from src import brain_map
from src.skeptic_agent import DEFAULT_MODEL_PATH, FEATURE_NAMES

MIN_TRAINING_ROWS = 30      # below this a forest is a coin with extra steps
MIN_LOSS_ROWS = 5           # need at least a handful of the minority class
MIN_BALANCED_ACCURACY = 0.60  # ship gate: below this the skeptic is noise
HOLDOUT_FRACTION = 0.25
RANDOM_SEED = 42            # deterministic runs, reproducible metrics
N_ESTIMATORS = 200

META_PATH = DEFAULT_MODEL_PATH.with_name("skeptic_model_meta.json")


def load_training_rows(conn) -> list:
    """Every resolved simulated trade, as plain dicts."""
    try:
        rows = conn.execute(
            "SELECT underlying, strategy, proposed_on, expiry, vix, "
            "net_credit, net_debit, spread_width, max_loss, lots, result "
            "FROM simulated_trades").fetchall()
    except Exception:
        return []
    return [dict(r) for r in rows]


def row_features(row: dict) -> list:
    """One simulated_trades row -> the frozen FEATURE_NAMES vector.
    Mirrors skeptic_agent.generate_features for the market slots; the
    graph slots are zero by construction (see module docstring)."""
    net_credit = row.get("net_credit")
    net = (float(net_credit) if net_credit is not None
           else -float(row.get("net_debit") or 0.0))
    try:
        dte = float((date.fromisoformat(row["expiry"])
                     - date.fromisoformat(row["proposed_on"])).days)
    except (TypeError, ValueError):
        dte = 0.0
    features = [
        0.0,                                   # graph_edge_count
        0.0,                                   # graph_cum_confidence
        0.0,                                   # graph_avg_confidence
        0.0,                                   # graph_avg_r_multiple
        float(row.get("vix") or 0.0),          # vix
        net,                                   # net_credit_or_debit
        float(row.get("spread_width") or 0.0),
        dte,                                   # days_to_expiry
        float(row.get("max_loss") or 0.0),     # max_loss_per_lot
        float(row.get("lots") or 1.0),         # lots
    ]
    assert len(features) == len(FEATURE_NAMES)
    return features


def features_and_labels(rows: list) -> tuple:
    """(X, y) in FEATURE_NAMES order; scratch rows dropped."""
    X, y = [], []
    for row in rows:
        result = (row.get("result") or "").lower()
        if result not in ("win", "loss"):
            continue
        X.append(row_features(row))
        y.append(1 if result == "win" else 0)
    return X, y


def train_model(X: list, y: list, seed: int = RANDOM_SEED) -> tuple:
    """Fit + evaluate. Returns (model, metrics). Raises ValueError when
    the data is too thin to train honestly — callers surface the message
    instead of shipping a garbage model."""
    n, losses, wins = len(y), y.count(0), y.count(1)
    if n < MIN_TRAINING_ROWS:
        raise ValueError(
            f"only {n} labeled rows — need >= {MIN_TRAINING_ROWS}. Run a "
            "longer simulation range first (python3 -m src.simulator).")
    if losses < MIN_LOSS_ROWS or wins < MIN_LOSS_ROWS:
        raise ValueError(
            f"class too thin (wins={wins}, losses={losses}; need >= "
            f"{MIN_LOSS_ROWS} each) — the forest can't learn a class it "
            "has barely seen.")

    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import balanced_accuracy_score, confusion_matrix
    from sklearn.model_selection import train_test_split

    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y, test_size=HOLDOUT_FRACTION, stratify=y, random_state=seed)
    probe = RandomForestClassifier(
        n_estimators=N_ESTIMATORS, class_weight="balanced",
        random_state=seed)
    probe.fit(X_tr, y_tr)
    pred = probe.predict(X_te)
    metrics = {
        "rows": n, "wins": wins, "losses": losses,
        "holdout_rows": len(y_te),
        "holdout_accuracy": round(float(
            sum(p == t for p, t in zip(pred, y_te)) / len(y_te)), 4),
        "holdout_balanced_accuracy": round(
            float(balanced_accuracy_score(y_te, pred)), 4),
        "holdout_confusion_matrix": confusion_matrix(
            y_te, pred, labels=[0, 1]).tolist(),   # rows: true loss, win
    }

    # ship the forest refit on EVERYTHING — holdout was for the report
    model = RandomForestClassifier(
        n_estimators=N_ESTIMATORS, class_weight="balanced",
        random_state=seed)
    model.fit(X, y)
    return model, metrics


def save_model(model, metrics: dict, model_path=None, meta_path=None) -> None:
    model_path = model_path or DEFAULT_MODEL_PATH
    meta_path = meta_path or model_path.with_name(model_path.stem + "_meta.json")
    model_path.parent.mkdir(parents=True, exist_ok=True)
    with open(model_path, "wb") as f:
        pickle.dump(model, f)
    meta = dict(metrics, feature_names=list(FEATURE_NAMES),
                trained_at=datetime.now().isoformat(timespec="seconds"),
                n_estimators=N_ESTIMATORS, seed=RANDOM_SEED)
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)


def run_training(conn=None, model_path=None, dry_run: bool = False,
                 force: bool = False) -> dict:
    """The full pipeline. Returns the metrics dict (raises ValueError on
    too-thin data). Injectable conn/path for offline tests.

    THE SHIP GATE: a model whose holdout balanced accuracy is below
    MIN_BALANCED_ACCURACY is evaluated but NOT persisted (metrics carry
    shippable/saved=False) — the skeptic keeps abstaining, by the same
    doctrine written into its scaffolding: a noise forest warning on
    every proposal trains the human to ignore the real warnings later.
    `force=True` overrides for deliberate experiments only."""
    owns = conn is None
    if conn is None:
        conn = brain_map.connect()
    try:
        rows = load_training_rows(conn)
    finally:
        if owns:
            conn.close()
    X, y = features_and_labels(rows)
    model, metrics = train_model(X, y)
    metrics["shippable"] = (
        metrics["holdout_balanced_accuracy"] >= MIN_BALANCED_ACCURACY)
    metrics["saved"] = False
    if not dry_run and (metrics["shippable"] or force):
        save_model(model, metrics, model_path=model_path)
        metrics["saved"] = True
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Phase 7b: train the skeptic's Random Forest on "
                    "simulated trades")
    parser.add_argument("--dry-run", action="store_true",
                        help="evaluate only; write no model file")
    parser.add_argument("--force", action="store_true",
                        help="persist even below the balanced-accuracy ship "
                             "gate (experiments only)")
    args = parser.parse_args()
    try:
        m = run_training(dry_run=args.dry_run, force=args.force)
    except ValueError as e:
        raise SystemExit(f"Not training: {e}")
    print(json.dumps(m, indent=2))
    if m["saved"]:
        print(f"Model saved to {DEFAULT_MODEL_PATH} — the skeptic is LIVE: "
              "proposals below P(win) 0.40 now carry the ⚠️ warning.")
    elif not args.dry_run:
        print(f"NOT saved: holdout balanced accuracy "
              f"{m['holdout_balanced_accuracy']:.3f} < "
              f"{MIN_BALANCED_ACCURACY:g} ship gate — the skeptic keeps "
              "abstaining (a noise model is worse than none). Grow or "
              "enrich the simulated set and retrain, or --force for "
              "experiments.")
