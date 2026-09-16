"""
RULE 6 (2026-09-16, ledger Issue 29): the post-mortem analyst is a LIVE
Gemini call reached by the tracker's resolution path, and a dozen tests
resolve trades without stubbing it. From inside pytest the door must return
None WITHOUT dialling out — the night Gemini answered in 146 s per call the
suite took 24 minutes instead of 4.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import analyst


def test_post_mortem_door_is_muzzled_under_pytest(monkeypatch):
    calls = []
    monkeypatch.setattr(analyst, "_call_gemini", lambda *a, **k: calls.append(a) or {})
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")
    out = analyst.generate_post_mortem({"ticker": "X"}, {"trigger": "stop_hit"})
    assert out is None
    assert calls == []                                   # never reached the network door


def test_the_muzzle_is_the_pytest_marker_not_the_key(monkeypatch):
    """Outside pytest the same function must still try the door: prove the
    gate is PYTEST_CURRENT_TEST by removing it and seeing the stub called."""
    seen = []
    monkeypatch.setattr(analyst, "_call_gemini", lambda prompt, key: seen.append(key) or
                        {"variance_analysis": "v", "unexpected_variables": "u",
                         "future_guardrails": "g"})
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    out = analyst.generate_post_mortem({"ticker": "X"}, {"trigger": "stop_hit"})
    assert seen == ["k"] and isinstance(out, dict)
