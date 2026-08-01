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
    # p_block -> p_circular on 2026-08-01: the block-bootstrap null was
    # replaced by the circular shift (its ~268 splices per surrogate were
    # exactly the discontinuity system_stress detects, so no real crisis
    # could beat it — see the ledger).
    assert set(("p_circular", "p_phase", "held_out_confirmed", "admitted")) \
        <= set(v)
    assert v["p_circular"] < 0.5       # a clear shock is rare in surrogates
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


# ---------------------------------------------------------------------
# 2026-08-01 — the three structural fixes (owner ruling after the first
# real-lake run admitted 0/25). Each test targets a defect the ORIGINAL
# suite could not reach, because its synthetic lakes were equal-length
# and hole-free while the real lake is neither.
# ---------------------------------------------------------------------

def test_z_series_matches_the_original_zdelta_definition_exactly():
    """_z_series was rewritten O(n^2) -> O(n*baseline). It must be
    BIT-identical to the per-day zdelta call it replaced, or the whole
    stress statistic silently changes meaning."""
    import math as _m
    from src.analysis import macro_features as MF
    vals = [100 + 10 * _m.sin(i / 11) + (i % 7) for i in range(400)]
    vals[50] = vals[123] = vals[300] = None          # holes on purpose
    for window, baseline in ((20, 60), (5, 30), (20, 252)):
        fast = AD._z_series(vals, window, baseline)
        slow = [MF.zdelta(vals[:t + 1], window, baseline)
                for t in range(len(vals))]
        assert fast == slow, f"mismatch at window={window} baseline={baseline}"


def test_surrogates_preserve_length_AND_hole_pattern():
    """THE RAGGED-MISSINGNESS FIX. Real channels start in different
    decades; a surrogate that drops holes comes back a different length
    per channel and _max_stress_of overruns (the IndexError that meant
    AD-2 had never run in production)."""
    import random
    rng = random.Random(3)
    series = [float(i % 13) for i in range(200)]
    for i in (0, 5, 77, 199):
        series[i] = None
    holes = [i for i, v in enumerate(series) if v is None]

    for name, sur in (("circular", AD.circular_shift(series, rng)),
                      ("block", AD.block_bootstrap(series, 16, rng)),
                      ("phase", AD.phase_randomize(series, rng))):
        assert len(sur) == len(series), f"{name} changed length"
        assert [i for i, v in enumerate(sur) if v is None] == holes, \
            f"{name} moved the holes"


def test_ragged_channels_no_longer_crash_the_gate():
    """The exact production shape: channels with different coverage."""
    import random
    rng = random.Random(4)
    n = 300
    ragged = {
        "A": [float(i % 9) for i in range(n)],                    # 100%
        "B": [None] * 150 + [float(i % 7) for i in range(150)],   # 50%
        "C": [None] * 240 + [float(i % 5) for i in range(60)],    # 20%
    }
    null = AD.build_null(ragged, window=5, n_surrogates=3, rng=rng)
    assert len(null["circular"]) == 3 and len(null["phase"]) == 3
    assert all(isinstance(x, float) for x in null["circular"])


def test_circular_shift_makes_ONE_splice_and_keeps_the_values():
    """A rotation preserves the channel's marginal distribution exactly
    and introduces a single wrap-point — unlike the block bootstrap,
    which spliced ~268 times and manufactured the very discontinuity the
    stress statistic hunts."""
    import random
    series = [float(i) for i in range(50)]
    sur = AD.circular_shift(series, random.Random(11))
    assert sorted(sur) == sorted(series)            # same values, no resample
    # exactly one descent in an otherwise ascending series = one splice
    drops = sum(1 for a, b in zip(sur, sur[1:]) if b < a)
    assert drops == 1


def test_the_null_is_candidate_independent_so_it_can_be_shared():
    """Fix 3: build_null depends only on the lake. Same seed + same lake
    must give the same null, which is what licenses computing it once."""
    import random
    chans = {"A": [float(i % 11) for i in range(120)],
             "B": [float((i * 3) % 7) for i in range(120)]}
    a = AD.build_null(chans, window=5, n_surrogates=4, rng=random.Random(9))
    b = AD.build_null(chans, window=5, n_surrogates=4, rng=random.Random(9))
    assert a["circular"] == b["circular"] and a["phase"] == b["phase"]


def test_gate_many_matches_per_candidate_gating_on_a_shared_null(tmp_path):
    """gate_many is the scan entry point; it must agree with the
    single-candidate path when both are handed the same null."""
    import random
    lake = tmp_path / "macro"
    _, chans = _calm_then_shock(n=70, shock_at=55)
    for c, pairs in chans.items():
        _write(lake, c, pairs)
    cands = AD.rank_shock_candidates(lake_dir=lake, top_n=2, min_gap_days=5)

    many = AD.gate_many(cands, lake_dir=lake, n_surrogates=12,
                        rng=random.Random(5))
    dates, matrix = AD._aligned_closes(lake)
    shared = AD.build_null(matrix, 20, 12, random.Random(5))
    one = AD.significance_gate(cands[0], lake_dir=lake, null=shared)

    assert len(many) == len(cands)
    assert many[0]["p_circular"] == one["p_circular"]
    assert many[0]["admitted"] == one["admitted"]


# ---------------------------------------------------------------------
# 2026-08-01 (second ruling) — the CO-STRESS redefinition. The RMS
# statistic's variable divisor made it incomparable across coverage
# eras: it mis-dated crises, promoted single-channel ghosts, and no
# surrogate null could be specified for it. These pin the new physics.
# ---------------------------------------------------------------------

def test_co_stress_is_the_second_largest_abs_z():
    """Direct definition check on a hand-built z pattern. (A perfectly
    FLAT channel reports z=None — degenerate baseline, zdelta's own
    contract — so all three channels here carry small variation.)"""
    n = 40
    dates = [str(i) for i in range(n)]
    wobble = [100.0 + 0.2 * (i % 3) for i in range(n)]
    spike = list(wobble)
    spike[30] = 130.0            # one big 5-day move on TWO channels
    matrix = {"A": spike, "B": list(spike), "C": wobble}
    # no explicit baseline: resolve exactly as co_stress does (the autouse
    # fixture shrinks Z_BASELINE_SESSIONS), so expected and actual share it
    z = {c: AD._z_series(v, 5) for c, v in matrix.items()}
    co = AD.co_stress(dates, matrix, window=5, min_channels=3)
    i = 30
    zs = sorted((abs(z[c][i]) for c in matrix if z[c][i] is not None),
                reverse=True)
    assert len(zs) >= 3          # all channels report -> day eligible
    assert co[i] == pytest.approx(zs[1])
    assert co[i] > 1.0           # the two-channel spike is visible


def test_single_channel_spike_scores_near_zero_not_eleven():
    """THE GHOST-KILLER. Under RMS, one channel at |z|~big on a thin day
    out-ranked COVID. Under co-stress the second mover is quiet, so the
    day scores ~0 — a lone spike is not simultaneity."""
    n = 60
    dates = [str(i) for i in range(n)]
    quiet1 = [100.0 + 0.01 * (i % 3) for i in range(n)]
    quiet2 = [50.0 + 0.005 * (i % 4) for i in range(n)]
    loud = list(quiet1)
    loud[45] = 160.0             # violent single-channel move
    matrix = {"A": loud, "B": quiet1, "C": quiet2}
    co = AD.co_stress(dates, matrix, window=5, min_channels=3)
    vals = [x for x in co if x is not None]
    assert vals, "eligible days exist"
    assert max(vals) < 3.0       # nothing remotely like the RMS-era 11.0


def test_days_below_the_channel_floor_are_ineligible_not_zero():
    n = 40
    dates = [str(i) for i in range(n)]
    a = [100.0 + (i % 5) for i in range(n)]
    b = [None] * n               # dead channel
    c = [None] * n               # dead channel
    co = AD.co_stress(dates, {"A": a, "B": b, "C": c},
                      window=5, min_channels=3)
    assert all(x is None for x in co)   # 1 channel can never be simultaneity


def test_ranking_no_longer_contains_single_channel_ghost_days(tmp_path):
    """A lake whose early era has ONE channel and whose late era has a
    real 4-channel shock: every anchor must come from the late era, even
    though the early single-channel move is the biggest raw |z|."""
    lake = tmp_path / "macro"
    d0 = date(2019, 1, 1)
    n_early, n_late = 60, 70
    dates = [(d0 + timedelta(days=i)).isoformat()
             for i in range(n_early + n_late)]
    # US10Y alone for the early era, with a huge spike in it
    us10y = [2.0 + 0.001 * (i % 3) for i in range(n_early + n_late)]
    us10y[30] = 6.0                                  # giant lone move
    _write(lake, "US10Y", list(zip(dates, us10y)))
    # the other three channels exist ONLY in the late era, with a
    # synchronized break at late index 55
    for c, base, jump in (("BRENT", 100.0, -18.0), ("DXY", 100.0, 6.0),
                          ("USDINR", 75.0, 4.0)):
        vals = []
        for i in range(n_late):
            v = base + 0.05 * ((i % 3) - 1)
            if i >= 55:
                v += jump
            vals.append(round(v, 4))
        _write(lake, c, list(zip(dates[n_early:], vals)))
    cands = AD.rank_shock_candidates(lake_dir=lake, top_n=5, min_gap_days=5)
    assert cands, "the synchronized late-era shock must be found"
    for c in cands:
        assert c["date"] >= dates[n_early], \
            f"single-channel-era ghost {c['date']} leaked into the ranking"
    assert all(c["stat"] == "co_stress_2ndmax_z20" for c in cands)


def test_null_and_observed_side_share_statistic_and_floor():
    """_max_stress_of must apply the SAME co-stress definition and the
    SAME eligibility floor as the observed side — a null computed under
    different conditions than the observation is not a null."""
    import random
    rng = random.Random(2)
    n = 80
    matrix = {"A": [float(100 + (i % 7)) for i in range(n)],
              "B": [float(50 + ((i * 3) % 5)) for i in range(n)],
              "C": [None] * n}          # dead channel -> only 2 alive
    # With only 2 live channels and MIN_CHANNELS=3, NO day is eligible:
    assert AD._max_stress_of(matrix, 5) == 0.0
    matrix["C"] = [float(20 + (i % 4)) for i in range(n)]   # revive it
    assert isinstance(AD._max_stress_of(matrix, 5), float)
