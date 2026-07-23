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
                        lambda templates, lake_dir=None, horizon="shock",
                        cache_path=None, require_cache=False:
                        {"e1": episode,
                         "e2": _rows([v * 1.01 for v in SHAPE]),
                         "e3": _rows([v * 0.99 for v in SHAPE])})
    # isolate declare's cache-status read from the real production cache
    monkeypatch.setattr(MR, "_load_fingerprint_cache",
                        lambda *a, **k: (None, "hit"))
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


def test_episode_fingerprints_uses_cache_without_recompute(tmp_path,
                                                           monkeypatch):
    """When the cache stamp matches the templates, episode_fingerprints
    returns the cached rows and NEVER calls the featurizer — the whole
    point of the e2-micro fix. Proven by making MF.trajectory explode."""
    import json
    templates = {"built_at": "STAMP-1",
                 "horizons": {"shock": {"episodes": [
                     {"name": "e1", "anchor": "2020-01-01",
                      "included": True}]}}}
    cache = tmp_path / "fp_cache.json"
    cache.write_text(json.dumps({
        "built_at": "STAMP-1",
        "horizons": {"shock": {"e1": _rows(SHAPE)}}}))

    def boom(*a, **k):
        raise AssertionError("featurizer must NOT run when cache is valid")
    monkeypatch.setattr(MR.MF, "trajectory", boom)

    out = MR.episode_fingerprints(templates, horizon="shock",
                                  cache_path=cache)
    assert out == {"e1": _rows(SHAPE)}


def test_episode_fingerprints_recomputes_when_cache_stale(tmp_path,
                                                          monkeypatch):
    """A cache stamped for a DIFFERENT build is ignored — the engine
    recomputes (correct, never wrong; just slower)."""
    import json
    templates = {"built_at": "STAMP-2",
                 "horizons": {"shock": {"episodes": [
                     {"name": "e1", "anchor": "2020-01-01",
                      "included": True}]}}}
    cache = tmp_path / "fp_cache.json"
    cache.write_text(json.dumps({
        "built_at": "STAMP-1",                       # mismatched stamp
        "horizons": {"shock": {"e1": [{"stale": 1}]}}}))
    called = []
    monkeypatch.setattr(MR.MF, "trajectory",
                        lambda *a, **k: called.append(1) or
                        {"rows": []})
    monkeypatch.setattr(MR.FP, "channel_rows",
                        lambda traj, channels=None: _rows(SHAPE))
    out = MR.episode_fingerprints(templates, horizon="shock",
                                  cache_path=cache)
    assert called and out == {"e1": _rows(SHAPE)}     # recomputed, not stale


def test_require_cache_raises_instead_of_recomputing(tmp_path):
    """The e2-micro fail-fast: with require_cache=True a stale/absent
    cache RAISES CacheUnavailable — it must NEVER fall into the 30-min
    recompute. MF.trajectory is booby-trapped to prove no recompute."""
    import json
    templates = {"built_at": "LIVE",
                 "horizons": {"shock": {"episodes": [
                     {"name": "e1", "anchor": "2020-01-01",
                      "included": True}]}}}
    cache = tmp_path / "fp.json"
    cache.write_text(json.dumps({"built_at": "OLD", "horizons": {}}))  # stale
    import pytest as _pt
    with _pt.raises(MR.CacheUnavailable) as ei:
        MR.episode_fingerprints(templates, horizon="shock",
                                cache_path=cache, require_cache=True)
    assert ei.value.status == "miss_stale"


def test_declare_abstains_and_screams_on_cache_miss(tmp_path, monkeypatch):
    """require_cache declare: a cache miss makes the horizon ABSTAIN with
    a named cache_miss reason + stamped status, never a recompute."""
    tpl, pb = _stub_environment(tmp_path, monkeypatch, _rows(SHAPE)[40:100])
    # a genuine miss: the status read reports it AND episode_fingerprints
    # raises (require_cache) exactly as the real code does on the VM
    monkeypatch.setattr(MR, "_load_fingerprint_cache",
                        lambda *a, **k: (None, "miss_absent"))

    def _raise(*a, **k):
        raise MR.CacheUnavailable("shock", "miss_absent")
    monkeypatch.setattr(MR, "episode_fingerprints", _raise)
    doc = MR.declare(templates_path=tpl, playbooks_path=pb,
                     state_path=tmp_path / "s.json",
                     ledger_path=tmp_path / "l.jsonl",
                     broadcast_fn=lambda p: None, require_cache=True)
    shock = doc["horizons"]["shock"]
    assert shock["declared"] is False
    assert shock["reason"] == "cache_miss_absent_aborted"
    assert shock["cache_status"] == "miss_absent"


def test_phase_mapping_edges():
    assert MR._phase_for(0) == "P1_shock"
    assert MR._phase_for(10) == "P1_shock"
    assert MR._phase_for(11) == "P2_basing"
    assert MR._phase_for(45) == "P2_basing"
    assert MR._phase_for(46) == "P3_resolution"
