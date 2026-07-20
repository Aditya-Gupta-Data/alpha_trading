"""Project-wide pytest config.

Centralizes the `slow` marker so the heavy statistical / ML / simulation files
can be skipped in the fast inner loop (`pytest -m "not slow"`) WITHOUT editing
each test file — this keeps their standalone `python tests/test_x.py` runners
working unchanged. The full `pytest` run still executes everything.

These files are the suite's real cost (the rest run in a few seconds total):
- test_noise_injection[_bars].py — Monte-Carlo false-discovery-rate regression
  (already CI-tuned to 8 seeds; the 25/500-seed runs live in the CLI).
- test_simulator.py            — replays historical bars through the real pipeline.
- test_train_skeptic.py        — fits a scikit-learn RandomForest.
- test_evolution.py            — backtest + Analyst/Critic dialectic per cluster.
"""

import pytest

_SLOW_FILES = {
    "test_noise_injection.py",
    "test_noise_injection_bars.py",
    "test_simulator.py",
    "test_train_skeptic.py",
    "test_evolution.py",
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.path.name in _SLOW_FILES:
            item.add_marker(pytest.mark.slow)
