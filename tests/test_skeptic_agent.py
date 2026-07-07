"""
Tests for the Phase 11 Random Forest Skeptic Agent scaffolding
(src/skeptic_agent.py): feature extraction merging graph + numerical
market data, the abstain-until-trained contract, and fail-safety.

Offline — no network, no sklearn required (the abstain path never imports
it; the inference path is tested with a stub model object).

Run:
    python tests/test_skeptic_agent.py
    pytest tests/test_skeptic_agent.py -v
"""

import pickle
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.skeptic_agent import (FEATURE_NAMES, WARN_BELOW_PROBABILITY,
                               RandomForestAuditor)

TODAY = date(2026, 7, 7)


def make_proposal(vix=13.5, net_credit=None, net_debit=62.0, width=200.0,
                  expiry="2026-07-16", max_loss=4650.0, lots=2):
    return {
        "action": "SPREAD", "ticker": "NIFTY 50", "view": "bullish",
        "vix": vix, "lots": lots,
        "spread": {"strategy": "bull_call_spread", "expiry": expiry,
                   "lot_size": 75, "lots": lots, "spread_width": width,
                   "net_credit": net_credit, "net_debit": net_debit,
                   "max_loss": max_loss, "max_profit": 10350.0, "legs": []},
    }


def make_graph_context():
    return [
        {"source": "bull_call_spread", "relation": "RESULTS_IN",
         "target": "win", "confidence_score": 0.9, "hops": 1},
        {"source": "win", "relation": "INDICATES",
         "target": "trend_follow", "confidence_score": 0.6, "hops": 2},
    ]


class StubModel:
    """Stands in for a trained sklearn forest: predict_proba + classes_."""

    def __init__(self, p_win):
        self.classes_ = [0, 1]
        self._p = p_win

    def predict_proba(self, X):
        assert len(X) == 1 and len(X[0]) == len(FEATURE_NAMES)
        return [[1 - self._p, self._p]]


def _auditor(model_path=None):
    # Point at a guaranteed-missing file unless a test supplies one, so the
    # real data/skeptic_model.pkl (if it ever exists) can't leak in.
    return RandomForestAuditor(model_path=model_path
                               or "/nonexistent/skeptic_model.pkl")


# --------------------------------------------------------------- features

def test_feature_vector_matches_frozen_contract():
    aud = _auditor()
    f = aud.generate_features(make_proposal(), make_graph_context(),
                              memory_stats={"avg_r_multiple": 1.4},
                              today=TODAY)
    assert len(f) == len(FEATURE_NAMES)
    by_name = dict(zip(FEATURE_NAMES, f))
    assert by_name["graph_edge_count"] == 2.0
    assert by_name["graph_cum_confidence"] == 1.5           # 0.9 + 0.6
    assert by_name["graph_avg_confidence"] == 0.75
    assert by_name["graph_avg_r_multiple"] == 1.4
    assert by_name["vix"] == 13.5
    assert by_name["net_credit_or_debit"] == -62.0          # debit -> negative
    assert by_name["spread_width"] == 200.0
    assert by_name["days_to_expiry"] == 9.0                 # 07-07 -> 07-16
    assert by_name["max_loss_per_lot"] == 4650.0
    assert by_name["lots"] == 2.0


def test_credit_spread_flips_premium_sign():
    p = make_proposal(net_credit=45.0, net_debit=None)
    f = _auditor().generate_features(p, today=TODAY)
    assert dict(zip(FEATURE_NAMES, f))["net_credit_or_debit"] == 45.0


def test_empty_graph_and_missing_values_degrade_to_zeros():
    p = make_proposal(vix=None, expiry=None)
    f = _auditor().generate_features(p, graph_context=None,
                                     memory_stats=None, today=TODAY)
    by_name = dict(zip(FEATURE_NAMES, f))
    assert by_name["graph_edge_count"] == 0.0
    assert by_name["graph_cum_confidence"] == 0.0
    assert by_name["graph_avg_confidence"] == 0.0
    assert by_name["graph_avg_r_multiple"] == 0.0
    assert by_name["vix"] == 0.0
    assert by_name["days_to_expiry"] == 0.0


def test_none_confidence_edges_do_not_crash():
    edges = [{"source": "a", "relation": "PRECEDES", "target": "b",
              "confidence_score": None, "hops": 1}]
    f = _auditor().generate_features(make_proposal(), edges, today=TODAY)
    by_name = dict(zip(FEATURE_NAMES, f))
    assert by_name["graph_edge_count"] == 1.0
    assert by_name["graph_cum_confidence"] == 0.0


# ------------------------------------------------- abstain-until-trained

def test_abstains_without_trained_model():
    aud = _auditor()
    f = aud.generate_features(make_proposal(), today=TODAY)
    assert aud.predict_win_probability(f) is None
    result = aud.audit(make_proposal(), make_graph_context(), today=TODAY)
    assert result["probability"] is None
    assert result["warn"] is False   # NEVER warn while untrained


def test_corrupt_model_file_abstains_not_raises():
    with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as tmp:
        tmp.write(b"not a pickle at all")
        path = tmp.name
    aud = _auditor(model_path=path)
    assert aud.predict_win_probability([0.0] * len(FEATURE_NAMES)) is None


# ----------------------------------------------------- inference contract

def test_stub_model_low_probability_warns():
    aud = _auditor()
    aud._model, aud._model_loaded = StubModel(p_win=0.20), True
    result = aud.audit(make_proposal(), make_graph_context(), today=TODAY)
    assert result["probability"] == 0.20
    assert result["warn"] is True


def test_stub_model_healthy_probability_stays_quiet():
    aud = _auditor()
    aud._model, aud._model_loaded = StubModel(p_win=0.65), True
    result = aud.audit(make_proposal(), make_graph_context(), today=TODAY)
    assert result["probability"] == 0.65
    assert result["warn"] is False
    assert WARN_BELOW_PROBABILITY <= 0.65


def test_pickled_stub_roundtrips_through_model_path():
    """The load path itself: a pickled model at model_path is found, loaded
    once, and used (StubModel is picklable — no sklearn needed)."""
    with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as tmp:
        pickle.dump(StubModel(p_win=0.30), tmp)
        path = tmp.name
    aud = _auditor(model_path=path)
    f = aud.generate_features(make_proposal(), today=TODAY)
    assert aud.predict_win_probability(f) == 0.30


# ------------------------------------------------------------- decoupling

def test_no_market_data_or_llm_imports():
    """Pure numerical module: no dhan/market/LLM/network imports, ever."""
    import src.skeptic_agent as sk
    source = Path(sk.__file__).read_text()
    import_lines = [l.strip() for l in source.splitlines()
                    if l.strip().startswith(("import ", "from "))]
    for line in import_lines:
        assert "dhan" not in line and "data_fetcher" not in line, line
        assert "rules" not in line and "notifier" not in line, line
        assert "httpx" not in line and "urllib" not in line, line
        assert "genai" not in line and "local_parser" not in line, line


if __name__ == "__main__":
    test_feature_vector_matches_frozen_contract()
    test_credit_spread_flips_premium_sign()
    test_empty_graph_and_missing_values_degrade_to_zeros()
    test_none_confidence_edges_do_not_crash()
    test_abstains_without_trained_model()
    test_corrupt_model_file_abstains_not_raises()
    test_stub_model_low_probability_warns()
    test_stub_model_healthy_probability_stays_quiet()
    test_pickled_stub_roundtrips_through_model_path()
    test_no_market_data_or_llm_imports()
    print("All skeptic agent tests passed.")
