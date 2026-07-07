"""
Alpha Trading — Phase 11 scaffolding: the Random Forest Skeptic Agent
=====================================================================

A quantitative auditor for the Knowledge Graph's semantic reasoning. The
graph (Phase 6C/6D) says things like "iron_condor RESULTS_IN loss when
VIX > 20" in words; this agent checks the same proposal with NUMBERS — a
Random Forest over a fixed feature vector that merges:

  * the 2-hop BFS graph context (src/graph_engine.py output): how much
    linked history exists and how confident it is, plus the Brain Map's
    realized stats for the active pattern tags, and
  * the proposal's own market numbers: India VIX, net premium, spread
    width, days to expiry, max loss, lots.

SCAFFOLDING STATUS (important): the model is NOT trained yet — training
data comes from the Phase 7 Time-Travel Simulator, which doesn't exist
yet. Until a trained model file appears at data/skeptic_model.pkl, the
auditor ABSTAINS: predict_win_probability() returns None and no warning
is ever emitted. An untrained forest would be noise, and a fake warning
on every proposal would train the human to ignore the real ones later.

Guardrails:
  * ADVISORY ONLY (decision #33's spirit): the skeptic warns in the alert
    text; it never gates, resizes, or rejects a proposal. Human decides.
  * PURE NUMERICAL MODULE: no market-data imports (dhan_client etc.), no
    network, no LLM — callers pass all inputs in. Same decoupling
    discipline as decision #30's parser rule.
  * FAIL-SAFE: sklearn is imported lazily only when a trained model file
    actually exists; any failure (sklearn missing, corrupt pickle) makes
    the auditor abstain, never raise into the proposal path.

Feature order is FROZEN in FEATURE_NAMES — the Phase 7 training pipeline
must build its X matrix in exactly this order, or the model will silently
score garbage. Change the tuple = retrain the model.
"""

import pickle
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL_PATH = ROOT / "data" / "skeptic_model.pkl"

# The frozen feature contract (see module docstring). One float per name.
FEATURE_NAMES = (
    "graph_edge_count",        # how many linked edges the 2-hop BFS found
    "graph_cum_confidence",    # sum of edge confidence scores
    "graph_avg_confidence",    # mean edge confidence (0 when no edges)
    "graph_avg_r_multiple",    # Brain Map avg R for the active tags (0 if none)
    "vix",                     # India VIX at proposal time (0 if unavailable)
    "net_credit_or_debit",     # per share: +credit / -debit
    "spread_width",            # strike width per share (worst-case side)
    "days_to_expiry",          # calendar days from today to expiry
    "max_loss_per_lot",        # defined-risk max loss, rupees per lot
    "lots",                    # position size
)

# Below this predicted win probability the proposer appends the ⚠️ Skeptic
# Agent Warning to the Discord alert (only ever reached with a REAL model).
WARN_BELOW_PROBABILITY = 0.40


def _num(value, default: float = 0.0) -> float:
    """Lenient float coercion — None/junk becomes `default`, never raises."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # NaN guard


class RandomForestAuditor:
    """Feature generation + (once trained) win-probability inference for
    one options proposal. Stateless between calls apart from the cached
    model; safe to construct anywhere — nothing heavy happens in init."""

    def __init__(self, model_path=None):
        self.model_path = Path(model_path) if model_path else DEFAULT_MODEL_PATH
        self._model = None
        self._model_loaded = False  # tried-and-failed also counts as loaded

    # ------------------------------------------------------------ features

    def generate_features(self, proposal: dict, graph_context: list = None,
                          memory_stats: dict = None, today: date = None) -> list:
        """Merge the graph's semantic evidence with the proposal's market
        numbers into the frozen FEATURE_NAMES vector (plain list of floats,
        no numpy needed).

        `proposal`      the options_proposer proposal dict (spread payload,
                        vix, lots).
        `graph_context` graph_engine.GraphEngine.get_relevant_context()
                        output — a list of {confidence_score, ...} edges.
        `memory_stats`  brain_map.query_similar_events() output — realized
                        {avg_r_multiple, ...} for the active pattern tags.
        `today`         injectable clock for days_to_expiry (tests)."""
        edges = graph_context or []
        confidences = [_num(e.get("confidence_score")) for e in edges]
        edge_count = float(len(edges))
        cum_conf = sum(confidences)
        avg_conf = (cum_conf / edge_count) if edge_count else 0.0
        avg_r = _num((memory_stats or {}).get("avg_r_multiple"))

        spread = proposal.get("spread") or {}
        net_credit = spread.get("net_credit")
        net = (_num(net_credit) if net_credit is not None
               else -_num(spread.get("net_debit")))

        today = today or date.today()
        try:
            dte = float((date.fromisoformat(spread.get("expiry")) - today).days)
        except (TypeError, ValueError):
            dte = 0.0

        features = [
            edge_count,
            round(cum_conf, 6),
            round(avg_conf, 6),
            avg_r,
            _num(proposal.get("vix")),
            net,
            _num(spread.get("spread_width")),
            dte,
            _num(spread.get("max_loss")),
            _num(proposal.get("lots"), 1.0),
        ]
        assert len(features) == len(FEATURE_NAMES)
        return features

    # ----------------------------------------------------------- inference

    def _load_model(self):
        """Lazy, one-shot model load. sklearn is only imported here — so
        until a trained model file exists, this module costs nothing (an
        e2-micro consideration) and works without sklearn installed at
        all. Any failure -> stay None (abstain)."""
        if self._model_loaded:
            return self._model
        self._model_loaded = True
        if not self.model_path.exists():
            return None
        try:
            import sklearn  # noqa: F401 -- unpickling the forest needs it
            with open(self.model_path, "rb") as f:
                self._model = pickle.load(f)
        except Exception as e:
            print(f"  (skeptic: model at {self.model_path.name} unusable: {e} "
                  "— abstaining)")
            self._model = None
        return self._model

    def predict_win_probability(self, features: list):
        """The trained forest's P(win) for one feature vector, or None to
        ABSTAIN (no trained model yet / any inference failure). The Phase 7
        simulator will train and persist the model; until then this always
        abstains by design."""
        model = self._load_model()
        if model is None:
            return None
        try:
            proba = model.predict_proba([list(features)])[0]
            classes = list(getattr(model, "classes_", [0, 1]))
            return float(proba[classes.index(1)])
        except Exception as e:
            print(f"  (skeptic: inference failed: {e} — abstaining)")
            return None

    def audit(self, proposal: dict, graph_context: list = None,
              memory_stats: dict = None, today: date = None) -> dict:
        """The one-call entry point the proposer uses. Never raises.
        Returns {"probability": float-or-None, "warn": bool,
                 "features": list, "feature_names": tuple}."""
        try:
            features = self.generate_features(proposal, graph_context,
                                              memory_stats, today=today)
            probability = self.predict_win_probability(features)
        except Exception as e:
            print(f"  (skeptic: audit skipped: {e})")
            return {"probability": None, "warn": False, "features": [],
                    "feature_names": FEATURE_NAMES}
        return {
            "probability": probability,
            "warn": (probability is not None
                     and probability < WARN_BELOW_PROBABILITY),
            "features": features,
            "feature_names": FEATURE_NAMES,
        }
