"""
AD-1 unsupervised discovery, fully offline: the shock scanner finds an
INJECTED multi-asset shock with no labels, the min-gap dedups one crisis
to one anchor, the motif scan finds a repeated slow pattern, the AD-2/3/4
scaffolds return honest not-built markers, and discover() writes (or
dry-runs) the candidate file. Baseline is shrunk so the rolling z is fast.
"""
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


def test_scaffolds_return_honest_not_built_markers():
    assert AD.significance_gate({"date": "2020-03-01"})["admitted"] is False
    assert "not_built" in AD.significance_gate({})["status"]
    assert "not_built" in AD.route_to_court([])["status"]
    assert "not_built" in AD.merged_catalog()["status"]


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
