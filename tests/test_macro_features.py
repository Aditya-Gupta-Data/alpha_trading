"""
Department 8 (Analysis) — macro_features, THE single macro featurizer.

Pins the honesty contract of docs/macro_regime_engine_spec.md §2 before any
fingerprint or state-tracker code consumes it:

  * zdelta matches a hand-computed z on a toy series, and returns None on
    insufficient history (abstention beats hallucination);
  * align forward-fills at most ffill_limit consecutive sessions and FLAGS
    every filled cell — no silent interpolation, no leading fills;
  * corr_state carries the right sign on constructed anti-correlated series
    and stays None until the window fills;
  * the Dollar-vs-Crude clash fires exactly on the versioned condition
    (corr60 < -0.4 AND |z20| > 1 BOTH legs, strict) and not at/below it;
  * feature_vector always names its holes (missing file, stale series).

Hermetic: synthetic series only, tmp_path lakes, zero network.
"""
import statistics
from datetime import date, timedelta

import pytest

from src.analysis import macro_features as MF

START = date(2025, 1, 1)


def _dates(n, start=START):
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


def _walk(start_value, factors):
    """start_value then start_value*f1, *f1*f2, ... (len = len(factors)+1)."""
    vals = [start_value]
    for f in factors:
        vals.append(vals[-1] * f)
    return vals


def _clash_pair(n_flat=280, n_trend=60, brent_factor=0.994):
    """DXY flat then +0.6%/day; BRENT flat then brent_factor/day.
    Default is the clash shape: dollar ripping, crude collapsing."""
    dxy = _walk(100.0, [1.0] * (n_flat - 1) + [1.006] * n_trend)
    brent = _walk(80.0, [1.0] * (n_flat - 1) + [brent_factor] * n_trend)
    ds = _dates(n_flat + n_trend)
    return (list(zip(ds, dxy)), list(zip(ds, brent)))


def _write_csv(lake, key, rows):
    with open(lake / f"{key}.csv", "w") as fh:
        fh.write("date,value\n")
        for d, v in rows:
            fh.write(f"{d},{'' if v is None else v}\n")


# ---------------------------------------------------------------- read_series

def test_read_series_missing_file_is_honest_empty_list(tmp_path):
    assert MF.read_series("DXY", lake_dir=tmp_path) == []


def test_read_series_parses_header_nulls_and_sorts(tmp_path):
    (tmp_path / "DXY.csv").write_text(
        "date,value\n"
        "2025-01-03,101.5\n"
        "2025-01-01,100.0\n"      # out of order -> sorted on read
        "2025-01-02,\n"           # empty value = NULL-honest hole
        "2025-01-04,NaN\n"        # NaN token = hole, never float('nan')
        "not-a-date,99\n"         # junk row skipped
    )
    assert MF.read_series("DXY", lake_dir=tmp_path) == [
        ("2025-01-01", 100.0),
        ("2025-01-02", None),
        ("2025-01-03", 101.5),
        ("2025-01-04", None),
    ]


# --------------------------------------------------------------------- zdelta

def test_zdelta_matches_hand_computed_value():
    # 252 one-day changes alternating exactly -1%, +1% (126 of each, ending
    # +1%): mean = 0, population std = 0.01, so the final change of +1%
    # hand-computes to z = (0.01 - 0) / 0.01 = 1.0.
    factors = [0.99 if i % 2 == 0 else 1.01 for i in range(252)]
    values = _walk(100.0, factors)
    assert MF.zdelta(values, 1) == pytest.approx(1.0, abs=1e-9)


def test_zdelta_window20_cross_checked_independently():
    import random
    r = random.Random(42)
    values = _walk(100.0, [1 + r.uniform(-0.01, 0.01) for _ in range(299)])
    changes = [(values[t] - values[t - 20]) / values[t - 20]
               for t in range(20, len(values))]
    tail = changes[-252:]
    expected = (changes[-1] - statistics.fmean(tail)) / statistics.pstdev(tail)
    assert MF.zdelta(values, 20) == pytest.approx(expected)


def test_zdelta_insufficient_history_is_none_never_a_guess():
    # 252 values -> only 251 one-day changes: one short of the 252 baseline.
    factors = [0.99 if i % 2 == 0 else 1.01 for i in range(251)]
    assert MF.zdelta(_walk(100.0, factors), 1) is None
    assert MF.zdelta([100.0, 101.0], 1) is None            # tiny series
    assert MF.zdelta([], 20) is None


def test_zdelta_hole_at_either_end_of_current_change_is_none():
    factors = [0.99 if i % 2 == 0 else 1.01 for i in range(260)]
    values = _walk(100.0, factors)
    broken = values[:-1] + [None]                          # today missing
    assert MF.zdelta(broken, 1) is None


def test_zdelta_degenerate_flat_baseline_is_none():
    assert MF.zdelta([100.0] * 300, 1) is None             # std == 0


# ---------------------------------------------------------------------- align

def test_align_ffill_stops_at_cap_and_flags_every_fill():
    ds = _dates(6)
    series = {
        "A": [(d, float(i)) for i, d in enumerate(ds)],   # full, drives union
        "B": [(ds[0], 10.0), (ds[1], None), (ds[4], 12.0)],
    }
    dates, matrix, flags = MF.align(series, ffill_limit=2)
    assert dates == ds
    assert matrix["A"] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    assert flags["A"] == [False] * 6
    # B: genuine, fill, fill, CAP -> hole, genuine resets, fill again
    assert matrix["B"] == [10.0, 10.0, 10.0, None, 12.0, 12.0]
    assert flags["B"] == [False, True, True, False, False, True]


def test_align_never_fills_leading_holes():
    ds = _dates(4)
    series = {
        "A": [(d, 1.0) for d in ds],
        "C": [(ds[2], 5.0)],
    }
    _, matrix, flags = MF.align(series, ffill_limit=2)
    assert matrix["C"] == [None, None, 5.0, 5.0]
    assert flags["C"] == [False, False, False, True]


# ----------------------------------------------------------------- corr_state

def test_corr_state_sign_on_constructed_anticorrelated_series():
    a = [float(i) for i in range(10)]
    b = [float(-i) for i in range(10)]
    out = MF.corr_state(a, b, window=5)
    assert out[:4] == [None, None, None, None]             # window not filled
    for r in out[4:]:
        assert r == pytest.approx(-1.0)
    out_pos = MF.corr_state(a, [x + 3.0 for x in a], window=5)
    assert out_pos[-1] == pytest.approx(1.0)


def test_corr_state_hole_in_window_and_zero_variance_are_none():
    a = [1.0, 2.0, 3.0, None, 5.0, 6.0, 7.0]
    b = [7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0]
    out = MF.corr_state(a, b, window=3)
    assert out[3] is None and out[4] is None and out[5] is None  # hole inside
    assert out[6] == pytest.approx(-1.0)                   # hole rolled out
    flat = MF.corr_state([2.0] * 6, b[:6], window=3)
    assert flat[-1] is None                                # no variance
    with pytest.raises(ValueError):
        MF.corr_state([1.0, 2.0], [1.0], window=2)         # not aligned


# ---------------------------------------------------------------------- clash

def test_clash_condition_exact_boundaries_strict():
    # At the thresholds exactly -> NOT a clash (spec says < -0.4 and > 1).
    assert MF.clash_condition(-0.4, 2.0, -2.0) is False
    assert MF.clash_condition(-0.41, 1.0, -2.0) is False
    assert MF.clash_condition(-0.41, 2.0, 1.0) is False
    # Just beyond both -> clash (|z| so a crashing series counts too).
    assert MF.clash_condition(-0.400001, 1.000001, -1.000001) is True
    # Missing evidence never declares a clash.
    assert MF.clash_condition(None, 2.0, -2.0) is False
    assert MF.clash_condition(-0.9, None, -2.0) is False
    assert MF.clash_condition(-0.9, 2.0, None) is False


def test_clash_fires_on_the_constructed_dollar_crude_divergence():
    dxy, brent = _clash_pair()
    as_of = dxy[-1][0]
    assert MF.clash(dxy, brent, as_of) is True


def test_clash_does_not_fire_when_they_move_together():
    # Same big |z20| on both legs, but co-moving -> corr60 ~ +1, no clash.
    dxy, brent = _clash_pair(brent_factor=1.006)
    assert MF.clash(dxy, brent, dxy[-1][0]) is False


def test_clash_does_not_fire_on_a_flat_leg_or_short_history():
    dxy, brent = _clash_pair()
    flat_brent = [(d, 80.0) for d, _ in brent]             # crude never moves
    assert MF.clash(dxy, flat_brent, dxy[-1][0]) is False
    short = 100                                            # < 252-change base
    assert MF.clash(dxy[:short], brent[:short], dxy[short - 1][0]) is False


# ------------------------------------------------------------- feature_vector

@pytest.fixture()
def lake(tmp_path):
    """Synthetic lake: DXY + BRENT in the clash shape, USDINR flat and
    STALE (stops 2 sessions early), US10Y/INDIAVIX/NIFTY absent."""
    dxy, brent = _clash_pair()
    _write_csv(tmp_path, "DXY", dxy)
    _write_csv(tmp_path, "BRENT", brent)
    usdinr = [(d, 83.0) for d, _ in dxy[:-2]]
    _write_csv(tmp_path, "USDINR", usdinr)
    return tmp_path, dxy, brent


def test_feature_vector_always_names_its_holes(lake):
    lake_dir, dxy, _ = lake
    fv = MF.feature_vector(dxy[-1][0], lake_dir=lake_dir)
    # Stale (USDINR) and missing (US10Y/INDIAVIX/NIFTY) are all named;
    # genuinely-observed series are not.
    assert fv["holes"] == ["USDINR", "US10Y", "INDIAVIX", "NIFTY"]
    fresh = MF.feature_vector(dxy[-3][0], lake_dir=lake_dir)
    assert "USDINR" not in fresh["holes"]                  # fresh at that date


def test_feature_vector_zscores_pairs_and_clash(lake):
    lake_dir, dxy, brent = lake
    as_of = dxy[-1][0]
    fv = MF.feature_vector(as_of, lake_dir=lake_dir)
    assert fv["as_of"] == as_of
    assert set(fv["series"]) == set(MF.SERIES)
    # Dollar ripping, crude collapsing: strong opposite-signed z on both.
    assert fv["series"]["DXY"]["z20"] > 1
    assert fv["series"]["BRENT"]["z20"] < -1
    assert fv["series"]["DXY"]["z60"] is not None
    # Missing series carry honest None features, not guesses.
    assert fv["series"]["US10Y"] == {"z20": None, "z60": None}
    # Flat USDINR has a degenerate baseline -> None, not 0.0.
    assert fv["series"]["USDINR"]["z20"] is None
    assert fv["pairs"]["dxy_brent_corr60"] < -0.4
    assert fv["pairs"]["dxy_brent_clash"] is True
    # One definition of the clash: the pair flag equals the clash() call.
    assert fv["pairs"]["dxy_brent_clash"] == MF.clash(dxy, brent, as_of)


# ----------------------------------------------------------------- trajectory

def test_trajectory_honest_nones_outside_history(lake):
    lake_dir, dxy, _ = lake
    calendar = [d for d, _ in dxy]
    anchor = calendar[5]
    traj = MF.trajectory(anchor, t_minus=10, t_plus=3, lake_dir=lake_dir)
    rows = traj["rows"]
    assert traj["anchor_session"] == anchor
    assert len(rows) == 14
    assert [r["offset"] for r in rows] == list(range(-10, 4))
    # Offsets before the first session are honest None rows...
    for r in rows[:5]:
        assert r["date"] is None and r["vector"] is None
    # ...and in-history rows come from the same featurizer (as_of stamped).
    assert rows[5]["date"] == calendar[0]
    assert rows[10]["date"] == anchor
    assert rows[10]["vector"]["as_of"] == anchor
    assert rows[10]["vector"]["holes"]                     # thin lake -> holes


def test_trajectory_end_of_history_and_weekend_anchor(lake):
    lake_dir, dxy, _ = lake
    calendar = [d for d, _ in dxy]
    day_after = (date.fromisoformat(calendar[-1]) +
                 timedelta(days=1)).isoformat()
    traj = MF.trajectory(day_after, t_minus=2, t_plus=3, lake_dir=lake_dir)
    assert traj["anchor_session"] == calendar[-1]          # snaps back
    assert [r["date"] for r in traj["rows"]] == [
        calendar[-3], calendar[-2], calendar[-1], None, None, None]
    before_history = (START - timedelta(days=30)).isoformat()
    void = MF.trajectory(before_history, t_minus=1, t_plus=1,
                         lake_dir=lake_dir)
    assert void["anchor_session"] is None
    assert all(r["vector"] is None for r in void["rows"])
