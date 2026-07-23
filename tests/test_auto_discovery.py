"""
AD-1 unsupervised discovery, fully offline: the shock scanner finds an
INJECTED multi-asset shock with no labels, the min-gap dedups one crisis
to one anchor, the motif scan finds a repeated slow pattern, the AD-2/3/4
scaffolds return honest not-built markers, and discover() writes (or
dry-runs) the candidate file. Baseline is shrunk so the rolling z is fast.
"""
import json
from datetime import date, timedelta

import pytest

from src.analysis import auto_discovery as AD
from src.analysis import macro_features as MF


@pytest.fixture(autouse=True)
def _fast_baseline(monkeypatch):
    # shrink the 252-session z baseline so a ~70-point synthetic lake
    # produces real z-scores in milliseconds
    monkeypatch.setattr(MF, "Z_BASELINE_SESSIONS", 15)


def _write(lake, key, pairs):
    lake.mkdir(parents=True, exist_ok=True)
    body = "date,value\n" + "".join(f"{d},{v}\n" for d, v in pairs)
    (lake / f"{key}.csv").write_text(body)


def _calm_then_shock(n=70, shock_at=55):
    """n daily points per channel: calm drift, then a sharp synchronized
    multi-asset move at `shock_at` — an unlabelled 'shock'."""
    d0 = date(2020, 1, 1)
    dates = [(d0 + timedelta(days=i)).isoformat() for i in range(n)]
    chans = {}
    for c, base, jump in (("BRENT", 100.0, -18.0), ("DXY", 100.0, 6.0),
                          ("USDINR", 75.0, 4.0), ("US10Y", 2.0, -0.6)):
        vals = []
        for i in range(n):
            v = base + (0.05 * ((i % 3) - 1))          # tiny wobble
            if i >= shock_at:
                v += jump                               # regime break
            vals.append(round(v, 4))
        chans[c] = list(zip(dates, vals))
    return dates, chans


def test_shock_scanner_finds_the_injected_shock_unlabelled(tmp_path):
    lake = tmp_path / "macro"
    _, chans = _calm_then_shock()
    for c, pairs in chans.items():
        _write(lake, c, pairs)
    cands = AD.rank_shock_candidates(lake_dir=lake, top_n=5, min_gap_days=5)
    assert cands, "scanner found no shock in a lake that clearly has one"
    top = date.fromisoformat(cands[0]["date"])
    # the peak stress lands in the post-break window, not the calm run
    assert top >= date(2020, 1, 1) + timedelta(days=55 - 3)
    assert cands[0]["stress"] > 1.0


def test_min_gap_collapses_one_crisis_to_one_anchor(tmp_path):
    lake = tmp_path / "macro"
    _, chans = _calm_then_shock()
    for c, pairs in chans.items():
        _write(lake, c, pairs)
    wide = AD.rank_shock_candidates(lake_dir=lake, top_n=10, min_gap_days=90)
    # a single injected shock -> exactly one anchor under a 90-day gap
    assert len(wide) == 1


def test_motif_scan_finds_a_repeated_slow_pattern(tmp_path):
    lake = tmp_path / "macro"
    d0 = date(2015, 1, 1)
    # a 10-long shape repeated twice with a gap, on one channel
    shape = [0.0, 1.0, 2.0, 3.0, 2.0, 1.0, 0.0, -1.0, -2.0, -1.0]
    seq = ([100.0] * 6 + [100 + s for s in shape] + [100.0] * 6
           + [100 + s for s in shape] + [100.0] * 6)
    dates = [(d0 + timedelta(days=i)).isoformat() for i in range(len(seq))]
    for c in ("BRENT", "DXY", "USDINR", "US10Y"):
        _write(lake, c, list(zip(dates, [round(v, 3) for v in seq])))
    pairs = AD.scan_motifs(lake_dir=lake, max_pairs=5, window=10, stride=3,
                           z_window=3)
    assert pairs, "no recurring window found in a lake with a clear repeat"
    assert pairs[0]["dtw"] is not None


def test_ad3_routes_only_admitted_to_discovered_episodes(tmp_path):
    out = tmp_path / "discovered.json"
    cands = [{"kind": "shock", "date": "2013-08-28", "admitted": True,
              "p_block": 0.01},
             {"kind": "shock", "date": "2017-01-01", "admitted": False}]
    doc = AD.route_to_court(cands, out_path=out)
    assert doc["n_admitted"] == 1 and doc["n_rejected"] == 1
    assert doc["episodes"][0]["anchor"] == "2013-08-28"
    assert doc["episodes"][0]["source"] == "auto"
    assert out.exists()


def test_ad4_merges_human_and_auto_and_flags_discoveries(tmp_path):
    human = tmp_path / "eps.yaml"
    human.write_text(
        "episodes:\n"
        "  - {anchor: 2020-02-24, name: covid, class: pandemic, why: a}\n")
    disc = tmp_path / "discovered.json"
    disc.write_text(json.dumps({"episodes": [
        {"name": "auto_shock_2020-03-01", "anchor": "2020-03-01",
         "source": "auto"},                     # near covid -> agreement
        {"name": "auto_shock_2011-08-08", "anchor": "2011-08-08",
         "source": "auto"}]}))                   # far -> a discovery
    cat = AD.merged_catalog(human_path=human, discovered_path=disc)
    assert cat["n_human"] == 1 and cat["n_auto"] == 2
    assert cat["n_discoveries"] == 1             # only the 2011 one is new
    by_name = {e.get("name"): e for e in cat["episodes"]}
    assert by_name["auto_shock_2020-03-01"]["discovery"] is False
    assert by_name["auto_shock_2011-08-08"]["discovery"] is True


def test_ad4_degrades_gracefully_when_files_missing(tmp_path):
    cat = AD.merged_catalog(human_path=tmp_path / "none.yaml",
                            discovered_path=tmp_path / "none.json")
    assert cat["n_human"] == 0 and cat["n_auto"] == 0


# ------------------------------------------------ AD-2 significance layer

def test_surrogate_primitives_preserve_the_right_invariants():
    import random
    rng = random.Random(1)
    series = [__import__("math").sin(i / 7) + 0.1 * (i % 3)
              for i in range(128)]
    bb = AD.block_bootstrap(series, 16, rng)
    assert len(bb) == len(series)                    # same length
    pr = AD.phase_randomize(series, rng)
    m0, s0 = AD._mean_std(series)
    m1, s1 = AD._mean_std(pr)
    assert len(pr) == len(series)
    assert abs(m0 - m1) < 1e-6 and abs(s0 - s1) < 1e-6  # spectrum preserved
    # p-value direction + add-one flooring
    assert AD.surrogate_pvalue(10, [1, 2, 3], "high") == 0.25
    assert AD.surrogate_pvalue(0.1, [1, 2, 3], "low") == 0.25
    assert AD.surrogate_pvalue(5, [], "high") == 1.0


def test_significance_gate_admits_a_real_shock_and_reports_both_nulls(
        tmp_path):
    lake = tmp_path / "macro"
    _, chans = _calm_then_shock(n=70, shock_at=55)
    for c, pairs in chans.items():
        _write(lake, c, pairs)
    top = AD.rank_shock_candidates(lake_dir=lake, top_n=1, min_gap_days=5)[0]
    import random
    v = AD.significance_gate(top, lake_dir=lake, n_surrogates=40,
                             rng=random.Random(0))
    assert set(("p_block", "p_phase", "held_out_confirmed", "admitted")) \
        <= set(v)
    assert v["p_block"] < 0.5          # a clear shock is rare in surrogates
    assert isinstance(v["admitted"], bool)


def test_significance_gate_rejects_a_flat_noise_lake(tmp_path):
    """No real shock -> the strongest 'candidate' must NOT clear both
    nulls; the engine abstains rather than hallucinate a regime."""
    import random
    lake = tmp_path / "macro"
    rng = random.Random(7)
    d0 = date(2020, 1, 1)
    dates = [(d0 + timedelta(days=i)).isoformat() for i in range(80)]
    for c, base in (("BRENT", 100.0), ("DXY", 100.0),
                    ("USDINR", 75.0), ("US10Y", 2.0)):
        _write(lake, c, [(d, round(base + rng.gauss(0, 0.2), 4))
                         for d in dates])
    cands = AD.rank_shock_candidates(lake_dir=lake, top_n=1, min_gap_days=5)
    if cands:                                   # pure noise may still peak
        v = AD.significance_gate(cands[0], lake_dir=lake, n_surrogates=40,
                                 rng=random.Random(0))
        assert v["admitted"] is False           # noise is rejected


def test_significance_gate_motif_kind_is_pending_not_admitted(tmp_path):
    v = AD.significance_gate({"kind": "motif", "a": [], "b": []},
                             lake_dir=tmp_path)
    assert v["admitted"] is False and v["status"] == "motif_gate_pending"


def test_discover_writes_candidates_and_dry_run_does_not(tmp_path):
    lake = tmp_path / "macro"
    _, chans = _calm_then_shock()
    for c, pairs in chans.items():
        _write(lake, c, pairs)
    out = tmp_path / "candidates.json"
    doc = AD.discover(lake_dir=lake, out_path=out, dry_run=True)
    assert "shock_candidates" in doc and not out.exists()
    AD.discover(lake_dir=lake, out_path=out)
    assert out.exists()
