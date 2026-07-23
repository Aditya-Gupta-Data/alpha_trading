"""
M2 fingerprint engine, fully offline: DTW separates constructed shock
families and tolerates time-stretch, dark cells bridge at the documented
penalty (never as fake similarity), not-comparable pairs answer None and
never merge, the k-cap holds, the catalog refuses malformed rows, and
the artifact is deterministic and names its exclusions.
"""
import json

import pytest

from src.analysis import macro_fingerprints as FP


# ---------------------------------------------------------- fixtures

def _rows(pattern, channel="BRENT:z20"):
    """A fingerprint: one observed channel tracing `pattern`."""
    return [{channel: v} for v in pattern]


SPIKE = [0.0] * 5 + [3.0, 2.5, 2.0, 1.5, 1.0] + [0.5] * 5   # shock-and-decay
FLAT = [0.1, -0.1] * 7 + [0.1]                              # nothing happened


# ------------------------------------------------------------- units

def test_identical_fingerprints_have_zero_distance_full_coverage():
    d, cov = FP.dtw_distance(_rows(SPIKE), _rows(SPIKE))
    assert d == 0.0 and cov == 1.0


def test_dtw_tolerates_time_stretch_but_separates_shapes():
    """The same shock played out slower must stay closer than a genuinely
    different regime — the whole reason the primitive is DTW."""
    stretched = [v for v in SPIKE for _ in (0, 1)][:len(SPIKE) + 5]
    d_same, _ = FP.dtw_distance(_rows(SPIKE), _rows(stretched))
    d_diff, _ = FP.dtw_distance(_rows(SPIKE), _rows(FLAT))
    assert d_same < d_diff


def test_dark_cells_bridge_at_the_penalty_not_as_similarity():
    """One unobservable stretch must not sever two long fingerprints —
    but it costs the documented penalty and drops coverage below 1."""
    holey = _rows(SPIKE)
    holey[7] = {}                                # one dark offset
    d, cov = FP.dtw_distance(_rows(SPIKE), holey)
    assert d is not None and d > 0.0             # the bridge was not free
    assert cov < 1.0                             # and it is VISIBLE


def test_no_shared_observation_is_not_comparable():
    a = _rows(SPIKE, channel="BRENT:z20")
    b = _rows(SPIKE, channel="USDINR:z20")       # same shape, no shared channel
    d, cov = FP.dtw_distance(a, b)
    assert d is None and cov == 0.0
    assert FP.dtw_distance([], _rows(SPIKE)) == (None, 0.0)


def test_local_cost_uses_only_shared_channels():
    a = {"BRENT:z20": 1.0, "USDINR:z20": 9.0}
    b = {"BRENT:z20": 2.0, "US10Y:z20": -9.0}
    cost, shared = FP._local_cost(a, b)
    assert cost == 1.0 and shared == 1           # the 9s never entered


# -------------------------------------------------------- clustering

def _family_fixtures():
    """Six fingerprints, two constructed families (oil-shock vs flat)."""
    fps = {}
    for i, stretch in enumerate((0, 2, 4)):
        pat = [v for v in SPIKE for _ in range(1)][stretch:] + [0.0] * stretch
        fps[f"oil_{i}"] = _rows(pat)
    for i, wob in enumerate((0.1, 0.15, 0.2)):
        fps[f"flat_{i}"] = _rows([wob, -wob] * 7 + [wob])
    return fps


def test_cluster_recovers_the_constructed_families():
    dist, names = FP.distance_matrix(_family_fixtures())
    out = FP.cluster(dist, names, k_max=2)
    families = [set(c["members"]) for c in out]
    assert {"oil_0", "oil_1", "oil_2"} in families
    assert {"flat_0", "flat_1", "flat_2"} in families
    for c in out:
        assert c["medoid"] in c["members"]


def test_k_cap_holds_and_none_pairs_never_merge():
    fps = _family_fixtures()
    dist, names = FP.distance_matrix(fps)
    assert len(FP.cluster(dist, names, k_max=1)) == 1
    # an island observed on a channel nobody shares: None to everyone,
    # so it can NEVER merge — honest k_max overflow
    fps["island"] = _rows(SPIKE, channel="NIFTY:z20")
    dist, names = FP.distance_matrix(fps)
    out = FP.cluster(dist, names, k_max=1)
    assert len(out) == 2
    assert ["island"] in [c["members"] for c in out]


def test_cluster_is_deterministic():
    dist, names = FP.distance_matrix(_family_fixtures())
    assert FP.cluster(dist, names) == FP.cluster(dist, names)


# --------------------------------------------------- M2.1: core layer

def test_channel_rows_filters_to_the_requested_subset():
    traj = {"rows": [{"offset": 0, "date": "d", "vector": {
        "series": {"BRENT": {"z20": 1.0}, "NIFTY": {"z20": 9.0},
                   "INDIAVIX": {"z20": 8.0}},
        "pairs": {"dxy_brent_corr60": -0.5}}}]}
    full = FP.channel_rows(traj)
    core = FP.channel_rows(traj, channels=FP.CORE_CHANNELS)
    assert "NIFTY:z20" in full[0] and "NIFTY:z20" not in core[0]
    assert core[0] == {"BRENT:z20": 1.0, "dxy_brent_corr60": -0.5}


def test_india_only_divergence_cannot_reshuffle_the_taxonomy():
    """The addendum's exact pathology, as a regression test: two
    episodes identical on core channels but violently different on the
    India channels must still be ZERO apart at the clustering layer."""
    a = [{"BRENT:z20": v, "INDIAVIX:z20": 5.0} for v in SPIKE]
    b = [{"BRENT:z20": v, "INDIAVIX:z20": -5.0} for v in SPIKE]
    core_a = [{k: r[k] for k in r if k in FP.CORE_CHANNELS} for r in a]
    core_b = [{k: r[k] for k in r if k in FP.CORE_CHANNELS} for r in b]
    d_core, _ = FP.dtw_distance(core_a, core_b)
    d_full, _ = FP.dtw_distance(a, b)
    assert d_core == 0.0                      # taxonomy layer: identical
    assert d_full > 0.0                       # refinement layer: sees it


# ----------------------------------------------------------- catalog

def test_load_episodes_normalizes_and_refuses_malformed(tmp_path):
    good = tmp_path / "ok.yaml"
    good.write_text(
        "episodes:\n"
        "  - anchor: 2020-02-24\n    name: covid\n    class: pandemic\n"
        "    why: test\n")
    eps = FP.load_episodes(good)
    assert eps[0]["anchor"] == "2020-02-24" and eps[0]["name"] == "covid"

    bad = tmp_path / "bad.yaml"
    bad.write_text("episodes:\n  - anchor: 2020-02-24\n")   # no name
    with pytest.raises(ValueError):
        FP.load_episodes(bad)


# ---------------------------------------------------------- artifact

def _canned_trajectory(monkeypatch, shapes):
    """Patch the featurizer seam: anchor date -> canned channel rows."""
    def fake_trajectory(anchor, t_minus, t_plus, lake_dir=None):
        pat = shapes.get(anchor, [])
        return {"anchor": anchor, "anchor_session": anchor or None,
                "rows": [{"offset": i, "date": anchor,
                          "vector": None} for i in range(len(pat))]}
    # channel_rows reads vectors; feed it directly instead
    monkeypatch.setattr(FP.MF, "trajectory", fake_trajectory)
    monkeypatch.setattr(
        FP, "channel_rows",
        lambda traj, channels=None: shapes.get(traj["anchor"], []))


def test_build_templates_names_exclusions_and_is_deterministic(
        tmp_path, monkeypatch):
    catalog = tmp_path / "eps.yaml"
    catalog.write_text(
        "episodes:\n"
        "  - {anchor: 2020-02-24, name: covid, class: pandemic, why: a}\n"
        "  - {anchor: 2022-02-24, name: ukraine, class: geopolitical, why: b}\n"
        "  - {anchor: 1962-01-01, name: too_old, class: financial, why: c}\n")
    _canned_trajectory(monkeypatch, {
        "2020-02-24": _rows(SPIKE),
        "2022-02-24": _rows([v * 1.1 for v in SPIKE]),
        "1962-01-01": [{}] * 10,                  # fully dark window
    })
    out_path = tmp_path / "templates.json"
    doc = FP.build_templates(episodes_path=catalog, out_path=out_path,
                             k_max=1)
    shock = doc["horizons"]["shock"]
    assert [e["name"] for e in shock["excluded"]] == ["too_old"]
    flags = {e["name"]: e["included"] for e in doc["episodes"]}
    assert flags == {"covid": True, "ukraine": True, "too_old": False}
    assert len(shock["archetypes"]) == 1          # two spikes, one family
    assert set(shock["archetypes"][0]["members"]) == {"covid", "ukraine"}
    on_disk = json.loads(out_path.read_text())
    assert on_disk["horizons"]["shock"]["distances"] == shock["distances"]

    again = FP.build_templates(episodes_path=catalog,
                               out_path=out_path, dry_run=True, k_max=1)
    assert again["horizons"]["shock"]["distances"] == shock["distances"]
    assert again["horizons"]["shock"]["archetypes"] == shock["archetypes"]


def test_build_templates_dry_run_writes_nothing(tmp_path, monkeypatch):
    catalog = tmp_path / "eps.yaml"
    catalog.write_text(
        "episodes:\n"
        "  - {anchor: 2020-02-24, name: covid, class: pandemic, why: a}\n")
    _canned_trajectory(monkeypatch, {"2020-02-24": _rows(SPIKE)})
    out_path = tmp_path / "templates.json"
    FP.build_templates(episodes_path=catalog, out_path=out_path,
                       dry_run=True)
    assert not out_path.exists()


def test_horizons_never_cross_compare(tmp_path, monkeypatch):
    """A shock and a slow-burn episode with IDENTICAL shapes must land
    in separate horizon blocks with no pairwise distance between them —
    a war and a weather cycle are different species."""
    catalog = tmp_path / "eps.yaml"
    catalog.write_text(
        "episodes:\n"
        "  - {anchor: 2020-02-24, name: warlike, class: geopolitical,"
        " why: a}\n"
        "  - {anchor: 2023-06-01, name: nino, class: climate,"
        " horizon: slow_burn, why: b}\n")
    _canned_trajectory(monkeypatch, {"2020-02-24": _rows(SPIKE),
                                     "2023-06-01": _rows(SPIKE)})
    doc = FP.build_templates(episodes_path=catalog, dry_run=True)
    assert set(doc["horizons"]) == {"shock", "slow_burn"}
    assert doc["horizons"]["shock"]["distances"] == {}   # singleton each
    assert doc["horizons"]["slow_burn"]["distances"] == {}
    ids = [a["id"] for h in doc["horizons"].values()
           for a in h["archetypes"]]
    assert ids == ["A1", "S1"]        # separate namespaces, no mixing


def test_load_episodes_refuses_unknown_horizon(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("episodes:\n"
                   "  - {anchor: 2020-02-24, name: x, horizon: medium}\n")
    with pytest.raises(ValueError):
        FP.load_episodes(bad)
