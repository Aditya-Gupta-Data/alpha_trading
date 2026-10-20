"""Decision #152: per-strike GEX and the day profile (pure), and the read-only chain-lake backfill. Offline."""
import hashlib
import importlib.util
from pathlib import Path

import pytest

from src import lake
from src.analysis import gamma_profile as gp

ROOT = Path(__file__).resolve().parent.parent


def _node(gamma, oi, prev=None):
    n = {"oi": oi, "previous_oi": prev if prev is not None else oi, "greeks": {"gamma": gamma}}
    return n if gamma is not None else {"oi": oi, "greeks": {}}


def _oc(call_oi=None, put_oi=None, gamma=0.05, missing=()):
    """Strikes 95..105 around spot 100; call OI piles at 104, put OI at 96 unless overridden."""
    call_oi = call_oi or {104: 900_000}
    put_oi = put_oi or {96: 600_000}
    oc = {}
    for k in range(95, 106):
        g = None if k in missing else gamma * (1 - abs(k - 100) / 10)
        oc[f"{k:.6f}"] = {"ce": _node(g, call_oi.get(k, 50_000)),
                          "pe": _node(g, put_oi.get(k, 50_000))}
    return oc


def test_strike_gex_signs_and_units():
    rows = gp.strike_gex(_oc(), 100.0)
    assert [r["strike"] for r in rows] == list(map(float, range(95, 106)))
    r = next(x for x in rows if x["strike"] == 100.0)
    assert r["call_gex"] == pytest.approx(0.05 * 50_000 * 100.0 ** 2 * 0.01)        # rupees per 1% move
    assert r["put_gex"] == pytest.approx(-r["call_gex"])                            # naive: puts negative
    assert r["net_gex"] == pytest.approx(0.0) and r["abs_gex"] == pytest.approx(2 * r["call_gex"])
    assert gp.strike_gex({}, 100.0) == [] and gp.strike_gex(_oc(), None) == []


def test_a_strike_missing_gamma_is_incomplete_never_zero():
    rows = gp.strike_gex(_oc(missing=(99,)), 100.0)
    r = next(x for x in rows if x["strike"] == 99.0)
    assert r["call_gex"] is None and r["net_gex"] is None and r["complete"] is False


def test_profile_finds_walls_flip_and_a_positive_regime():
    p = gp.profile(gp.strike_gex(_oc(), 100.0), 100.0, 5.0)
    assert p["regime"] == "positive" and p["coverage"] == 1.0 and p["strikes_in_band"] == 11
    assert p["call_wall"] == 104.0 and p["put_wall"] == 96.0
    assert 96.0 < p["flip"] < 104.0 and p["spot_vs_flip_pct"] == pytest.approx((100 / p["flip"] - 1) * 100, abs=1e-3)
    assert 0 < p["net_share"] <= 1 and 0 < p["concentration"] < 1 and p["reason"] is None


def test_put_heavy_chains_read_negative():
    p = gp.profile(gp.strike_gex(_oc(put_oi={99: 3_000_000, 98: 2_000_000}), 100.0), 100.0, 5.0)
    assert p["regime"] == "negative" and p["net_gex"] < 0 and p["put_wall"] == 99.0


def test_low_coverage_abstains_by_name_and_the_band_bounds_the_strikes():
    p = gp.profile(gp.strike_gex(_oc(missing=(96, 97, 98, 99, 101)), 100.0), 100.0, 5.0)
    assert p["regime"] is None and "carry gamma and OI" in p["reason"] and p["coverage"] < gp.MIN_COVERAGE
    tight = gp.profile(gp.strike_gex(_oc(), 100.0), 100.0, 2.0)
    assert tight["strikes_in_band"] == 5 and tight["call_wall"] == 100.0       # 104's pile is out of band
    assert gp.profile(gp.strike_gex(_oc(), 100.0), 300.0, 5.0)["reason"].startswith("no strikes within")


def test_merge_sums_expiries_and_keeps_incompleteness():
    a, b = gp.strike_gex(_oc(), 100.0), gp.strike_gex(_oc(missing=(100,)), 100.0)
    m = {r["strike"]: r for r in gp.merge_rows(a, b)}
    one = {r["strike"]: r for r in a}
    assert m[104.0]["call_gex"] == pytest.approx(2 * one[104.0]["call_gex"])
    assert m[100.0]["complete"] is False and m[100.0]["net_gex"] is None


def test_oi_lot_multiple_tells_quantity_from_contracts():
    rows = gp.strike_gex(_oc(call_oi={104: 900_000}, put_oi={96: 600_000}), 100.0)
    assert gp.oi_lot_multiple(rows, 70_000) < 1.0
    assert gp.oi_lot_multiple(rows, 10_000) == 1.0
    assert gp.oi_lot_multiple(rows, None) is None


def test_the_module_is_pure():
    src = (ROOT / "src" / "analysis" / "gamma_profile.py").read_text()
    assert "import" not in src.replace("from __future__ import annotations", "")


# ---------------------------------------------------------------- backfill

def _backfill_module():
    spec = importlib.util.spec_from_file_location("backfill_gex", ROOT / "scripts" / "backfill_gex_profiles.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _digest(path: Path) -> str:
    return hashlib.sha256(b"".join(p.read_bytes() for p in sorted(path.rglob("*.gz")))).hexdigest()


def test_backfill_reads_chains_and_writes_only_gex(tmp_path):
    bf = _backfill_module()
    root = tmp_path / "lake"
    for day in ("2026-10-15", "2026-10-16"):
        lake.write_partition("chains/nifty", day, [
            {"underlying": "NIFTY 50", "expiry": "2026-10-20", "spot": 100.0, "vix": 14.2,
             "captured_at": f"{day}T15:41:00", "oc": _oc()},
            {"underlying": "NIFTY 50", "expiry": "2026-12-29", "spot": 100.0, "vix": 14.2,
             "captured_at": f"{day}T15:41:00", "oc": _oc(put_oi={99: 3_000_000})}], root=root)
    before = _digest(root / "chains")
    dry = bf.run(root=root, dry_run=True)
    assert not (root / "gex").exists() and len(dry["nifty"]) == 2
    out = bf.run(root=root)
    assert _digest(root / "chains") == before                      # the chain lake is never touched
    assert lake.list_days("gex/nifty", root) == ["2026-10-15", "2026-10-16"]
    row = lake.read_day("gex/nifty", "2026-10-16", root=root)[0]
    assert row["combined"]["expiries"] == 1                        # 2026-12-29 is beyond 35 days
    assert row["combined"]["profile"]["regime"] == "positive" and len(row["expiries"]) == 2
    assert row["expiries"][1]["profile"]["regime"] == "negative" and "naive" in row["sign_convention"]
    bf.run(root=root)                                              # a re-run replaces, never duplicates
    assert len(lake.read_day("gex/nifty", "2026-10-16", root=root)) == 1
    assert any(line.startswith("nifty") for line in bf.summary_lines(out))
