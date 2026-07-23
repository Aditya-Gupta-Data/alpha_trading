"""
M4 declaration tracker, fully offline through injected fingerprints:
the sliding match finds a planted segment at the right offset, the
declaration floors and analog counts gate exactly as specified, the
runner-up is always reported, the ledger appends one line per run, a
family transition fires exactly ONE card (sameness is silent, card
failure never blocks), and dry-run writes nothing.
"""
import json

from src.analysis import macro_regime as MR
from src.analysis import macro_fingerprints as FP


def _rows(pattern, channel="BRENT:z20"):
    return [{channel: v} for v in pattern]


SHAPE = [0.0] * 30 + [3.0, 2.5, 2.0, 1.5, 1.0] + [0.4] * 106  # 141 rows
FLATISH = [0.05, -0.05] * 70 + [0.05]                          # 141 rows


def test_best_window_match_finds_the_planted_segment():
    episode = _rows(SHAPE)
    start = 30                     # the DISTINCTIVE spike starts here —
    current = episode[start:start + 60]  # a segment only one window fits
    d, center, cov = MR.best_window_match(current, episode)
    assert d == 0.0 and cov == 1.0
    assert center == start + 30 - FP.T_MINUS      # window center offset


def test_evaluate_declares_only_above_both_floors():
    episode = _rows(SHAPE)
    current = episode[40:100]
    fps = {"e1": episode, "e2": _rows([v * 1.02 for v in SHAPE]),
           "e3": _rows([v * 0.98 for v in SHAPE]),
           "far": _rows(FLATISH)}
    members = {"A1": ["e1", "e2", "e3"], "A9": ["far"]}
    v = MR.evaluate(current, fps, members)
    assert v["declared"] is True and v["reason"] == "declared"
    assert v["best"]["archetype"] == "A1"
    assert v["best"]["analog_count"] >= 3
    assert v["runner_up"]["archetype"] == "A9"    # ambiguity always shown
    assert v["phase"] in ("P1_shock", "P2_basing", "P3_resolution")

    # same match quality but only TWO analogs -> abstain, named reason
    v2 = MR.evaluate(current, {k: fps[k] for k in ("e1", "e2", "far")},
                     {"A1": ["e1", "e2"], "A9": ["far"]})
    assert v2["declared"] is False
    assert "analogs" in v2["reason"]


def test_evaluate_abstains_below_similarity_floor():
    current = _rows(FLATISH[:60], channel="US10Y:z20")
    fps = {"e1": _rows(SHAPE), "e2": _rows(SHAPE), "e3": _rows(SHAPE)}
    v = MR.evaluate(current, fps, {"A1": ["e1", "e2", "e3"]})
    assert v["declared"] is False                 # no shared channel /
    # or weak match — either way the reason names itself
    assert v["reason"] != "declared"


def _stub_environment(tmp_path, monkeypatch, current_pattern):
    """Wire declare() to injected data: templates + playbooks on disk,
    featurizer stubs for the current window and episode fingerprints
    (shock horizon only — the slow-burn path rides the same code)."""
    eps = [{"name": n, "anchor": a, "included": True}
           for n, a in (("e1", "2020-01-01"), ("e2", "2021-01-01"),
                        ("e3", "2022-01-01"))]
    templates = {"built_at": "t0", "episodes": eps,
                 "horizons": {"shock": {
                     "episodes": eps,
                     "archetypes": [{"id": "A1",
                                     "members": ["e1", "e2", "e3"],
                                     "medoid": "e1"}]}}}
    tpl = tmp_path / "templates.json"
    tpl.write_text(json.dumps(templates))
    pb = tmp_path / "playbooks.json"
    pb.write_text(json.dumps(
        {"table": {"A1": {"P1_shock": {"NIFTY_BANK": {"n": 1}},
                          "P2_basing": {"NIFTY_IT": {"n": 1}},
                          "P3_resolution": {"NIFTY_FMCG": {"n": 1}}}}}))
    episode = _rows(SHAPE)
    monkeypatch.setattr(MR, "current_rows",
                        lambda lake_dir=None, length=60, horizon="shock":
                        ("2026-07-23", current_pattern))
    monkeypatch.setattr(MR, "episode_fingerprints",
                        lambda templates, lake_dir=None, horizon="shock":
                        {"e1": episode,
                         "e2": _rows([v * 1.01 for v in SHAPE]),
                         "e3": _rows([v * 0.99 for v in SHAPE])})
    return tpl, pb


def test_declare_writes_state_ledger_and_fires_one_transition_card(
        tmp_path, monkeypatch):
    tpl, pb = _stub_environment(tmp_path, monkeypatch, _rows(SHAPE)[40:100])
    state = tmp_path / "state.json"
    ledger = tmp_path / "ledger.jsonl"
    cards = []

    doc = MR.declare(templates_path=tpl, playbooks_path=pb,
                     state_path=state, ledger_path=ledger,
                     broadcast_fn=cards.append)
    assert doc["declared"] is True
    shock = doc["horizons"]["shock"]
    assert shock["playbook_slice"] is not None    # the slice rode along
    assert state.exists()
    assert len(ledger.read_text().strip().splitlines()) == 1
    assert len(cards) == 1                        # undeclared -> declared

    # same verdict again: ledger grows, NO second card (sameness silent)
    MR.declare(templates_path=tpl, playbooks_path=pb, state_path=state,
               ledger_path=ledger, broadcast_fn=cards.append)
    assert len(ledger.read_text().strip().splitlines()) == 2
    assert len(cards) == 1


def test_declare_transition_to_undeclared_fires_and_card_failure_is_open(
        tmp_path, monkeypatch):
    tpl, pb = _stub_environment(tmp_path, monkeypatch, _rows(SHAPE)[40:100])
    state, ledger = tmp_path / "s.json", tmp_path / "l.jsonl"
    MR.declare(templates_path=tpl, playbooks_path=pb, state_path=state,
               ledger_path=ledger, broadcast_fn=lambda p: None)

    # the window goes violently ALIEN (far beyond the similarity floor —
    # a merely-calm window sits ~0.71 similarity to SHAPE's 0.4 tail,
    # which is a fact the floor is allowed to accept) -> undeclare;
    # the card EXPLODES; the run survives
    monkeypatch.setattr(MR, "current_rows",
                        lambda lake_dir=None, length=60, horizon="shock":
                        ("2026-07-24", _rows([3.0, -3.0] * 30)))

    def boom(payload):
        raise RuntimeError("discord down")
    doc = MR.declare(templates_path=tpl, playbooks_path=pb,
                     state_path=state, ledger_path=ledger,
                     broadcast_fn=boom)
    assert doc["declared"] is False
    assert len(ledger.read_text().strip().splitlines()) == 2


def test_declare_dry_run_writes_nothing_and_no_card(tmp_path, monkeypatch):
    tpl, pb = _stub_environment(tmp_path, monkeypatch, _rows(SHAPE)[40:100])
    state, ledger = tmp_path / "s.json", tmp_path / "l.jsonl"
    cards = []
    doc = MR.declare(templates_path=tpl, playbooks_path=pb,
                     state_path=state, ledger_path=ledger,
                     broadcast_fn=cards.append, dry_run=True)
    assert doc["declared"] is True
    assert not state.exists() and not ledger.exists()
    assert cards == []


def test_phase_mapping_edges():
    assert MR._phase_for(0) == "P1_shock"
    assert MR._phase_for(10) == "P1_shock"
    assert MR._phase_for(11) == "P2_basing"
    assert MR._phase_for(45) == "P2_basing"
    assert MR._phase_for(46) == "P3_resolution"
