"""Hermetic tests for Black-Scholes Vanna/Charm (VEX/CEX), expiry-day T floor, OTM IV selection,
dealer_exposure_daily SQLite persistence, and Macro Lake US CPI / 10Y Breakeven / ICE DXY."""

import importlib.util
import json
import math
from pathlib import Path

import pytest

from src import brain_map, lake
from src.analysis import gamma_profile as gp
from src.analysis import gex_steering as gs
from src.ingestion import macro_lake as ml
from src.ingestion import participant_oi as poi

ROOT = Path(__file__).resolve().parent.parent


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_call_delta(S: float, K: float, iv_pct: float, t: float, r: float = gp.RISK_FREE) -> float:
    sigma = iv_pct / 100.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    return _ncdf(d1)


def _oc_with_otm_iv(spot=23_000.0, otm_iv=16.0, broken_itm_iv=0.0):
    """Build an option chain around `spot` where ITM sides have broken/corrupted IVs
    and OTM sides carry clean IVs."""
    t = 14.0 / 365.0
    oc = {}
    for k in range(22_400, 23_700, 100):
        g = gp.bs_gamma(spot, k, otm_iv, t)
        if k < spot:
            # Put is OTM (valid IV), Call is ITM (broken IV)
            ce_iv, pe_iv = broken_itm_iv, otm_iv
            ce_g, pe_g = 0.0, g
        else:
            # Call is OTM (valid IV), Put is ITM (broken IV)
            ce_iv, pe_iv = otm_iv, broken_itm_iv
            ce_g, pe_g = g, 0.0
        oc[f"{float(k):.6f}"] = {
            "ce": {"oi": 150_000, "previous_oi": 140_000, "implied_volatility": ce_iv, "greeks": {"gamma": ce_g}},
            "pe": {"oi": 120_000, "previous_oi": 110_000, "implied_volatility": pe_iv, "greeks": {"gamma": pe_g}},
        }
    return oc


def test_bs_vanna_and_charm_match_finite_difference_delta_derivatives():
    S, K, iv_pct, t = 23_000.0, 23_500.0, 18.0, 20.0 / 365.0
    vanna = gp.bs_vanna(S, K, iv_pct, t)
    charm = gp.bs_charm(S, K, iv_pct, t)
    assert vanna is not None and charm is not None

    # Finite difference dDelta / dSigma (where sigma = iv_pct / 100)
    h_iv = 1e-3  # 0.001 IV pct = 1e-5 in sigma
    d_hi = _bs_call_delta(S, K, iv_pct + h_iv, t)
    d_lo = _bs_call_delta(S, K, iv_pct - h_iv, t)
    fd_vanna = (d_hi - d_lo) / (2.0 * (h_iv / 100.0))
    assert vanna == pytest.approx(fd_vanna, rel=1e-5)

    # Finite difference -dDelta / dT (rate of change of delta as time passes -> T shrinks)
    h_t = 1e-6
    d_t_less = _bs_call_delta(S, K, iv_pct, t - h_t)
    d_t_more = _bs_call_delta(S, K, iv_pct, t + h_t)
    fd_charm = (d_t_less - d_t_more) / (2.0 * h_t)
    assert charm == pytest.approx(fd_charm, rel=1e-5)

    # Invalid inputs return None
    assert gp.bs_vanna(0, K, iv_pct, t) is None
    assert gp.bs_charm(S, K, 0, t) is None
    assert gp.bs_vanna(S, K, iv_pct, 0) is None


def test_expiry_day_t_floor_prevents_zero_or_none_on_0dte_and_rejects_past_dates():
    # On expiry day after 15:30 IST close (EOD capture at 15:40 IST): floored at T_FLOOR
    t_eod = gp.years_to_expiry("2026-10-29", "2026-10-29T15:40:00+05:30")
    assert t_eod == pytest.approx(gp.T_FLOOR)

    # On expiry day 5 minutes before 15:30 close (< 1 hour T_FLOOR): floored at T_FLOOR
    t_near_close = gp.years_to_expiry("2026-10-29", "2026-10-29T15:25:00+05:30")
    assert t_near_close == pytest.approx(gp.T_FLOOR)

    # On expiry day morning (09:30 IST = 6 hours to 15:30 > 1 hour T_FLOOR): exact time used
    t_morning = gp.years_to_expiry("2026-10-29", "2026-10-29T09:30:00+05:30")
    assert t_morning == pytest.approx(6.0 / (365.0 * 24.0))

    # Strictly past expiry date returns None
    assert gp.years_to_expiry("2026-10-28", "2026-10-29T09:30:00+05:30") is None

    # 0DTE repriced_profile succeeds on expiry day at 15:40 IST thanks to T_FLOOR
    oc = _oc_with_otm_iv()
    prof = gp.repriced_profile([(oc, "2026-10-29")], 23_000.0, "2026-10-29T15:40:00+05:30", 5.0, lot_size=65)
    assert prof["regime"] is not None
    assert prof["t_years"] == round(gp.T_FLOOR, 5)
    assert prof["net_vex"] is not None and prof["net_cex"] is not None


def test_otm_iv_selection_and_vex_cex_in_repriced_profile():
    oc = _oc_with_otm_iv(spot=23_000.0, otm_iv=16.0, broken_itm_iv=0.0)
    inputs = {r["strike"]: r for r in gp.strike_inputs(oc, 23_000.0)}

    for k, row in inputs.items():
        assert row["iv"] == 16.0
        assert row["iv_source"] == ("pe" if k < 23_000.0 else "ce")

    prof = gp.repriced_profile([(oc, "2026-11-12")], 23_000.0, "2026-10-29T15:40:00+05:30", 5.0, lot_size=65)
    assert prof["net_gex"] is not None and prof["net_gex_cr"] is not None
    assert prof["net_vex"] is not None and prof["abs_vex"] > 0
    assert prof["net_vex_cr"] == pytest.approx(prof["net_vex"] / 1e7, abs=1e-4)
    assert prof["net_cex"] is not None and prof["abs_cex"] > 0
    assert prof["net_cex_cr"] == pytest.approx(prof["net_cex"] / 1e7, abs=1e-4)
    assert prof["vex_regime"] in ("positive", "negative")
    assert prof["cex_regime"] in ("positive", "negative")


def test_backfill_writes_dealer_exposure_daily_idempotently(tmp_path):
    spec = importlib.util.spec_from_file_location("backfill_gex", ROOT / "scripts" / "backfill_gex_profiles.py")
    bf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bf)

    lake_root = tmp_path / "lake"
    db_path = tmp_path / "brain_map.db"
    oc = _oc_with_otm_iv()

    for day in ("2026-10-15", "2026-10-16"):
        lake.write_partition(
            "chains/nifty",
            day,
            [{
                "underlying": "NIFTY 50",
                "expiry": "2026-10-29",
                "spot": 23_000.0,
                "vix": 14.1,
                "captured_at": f"{day}T15:40:00+05:30",
                "oc": oc,
            }],
            root=lake_root,
        )

    # Dry run writes nothing to SQLite
    bf.run(root=lake_root, dry_run=True, db_path=db_path)
    assert not db_path.exists()

    # First run writes 2 daily rows into dealer_exposure_daily
    bf.run(root=lake_root, dry_run=False, db_path=db_path)
    conn = brain_map.connect(db_path)
    rows = conn.execute("SELECT * FROM dealer_exposure_daily ORDER BY date").fetchall()
    assert len(rows) == 2
    assert [r["date"] for r in rows] == ["2026-10-15", "2026-10-16"]
    assert rows[0]["slug"] == "nifty"
    assert rows[0]["underlying"] == "NIFTY 50"
    assert rows[0]["dealer"] == "naive"
    assert rows[0]["net_gex"] is not None
    assert rows[0]["net_vex"] is not None and rows[0]["net_vex_cr"] is not None
    assert rows[0]["net_cex"] is not None and rows[0]["net_cex_cr"] is not None

    # Re-run with calibrated participant OI updates in-place idempotently (still 2 rows)
    csv_text = (
        '""Participant wise Open Interest (no. of contracts) in Equity Derivatives as on Oct 16, 2026"",,,,,,,,,,,,,,\n'
        "Client Type,Future Index Long,Future Index Short,Future Stock Long,Future Stock Short,"
        "Option Index Call Long,Option Index Put Long,Option Index Call Short,Option Index Put Short,"
        "Option Stock Call Long,Option Stock Put Long,Option Stock Call Short,Option Stock Put Short,"
        "Total Long Contracts,Total Short Contracts\n"
        "Client,276618,56704,3453649,294863,3823581,4191151,3767056,4682637,2396725,1064078,1365136,1205337,15205802,11371733\n"
        "DII,51503,14310,352565,4680162,10575,89881,1900,0,5225,47310,428253,44127,557059,5168752\n"
        "FII,39966,312965,3616748,2840084,707527,1318296,981202,797059,310336,414657,467996,290447,6407530,5689753\n"
        "Pro,45888,29996,874777,482630,1223426,1247278,1014951,1366909,1119074,1103283,1569975,1089417,5613725,5553878\n"
        "TOTAL,413975,413975,8297739,8297739,5765109,6846605,5765109,6846605,3831360,2629328,3831360,2629328,27784116,27784116\n"
    )
    poi.run("2026-10-16", root=lake_root, fetch_fn=lambda d: (200, csv_text.encode()))
    bf.run(root=lake_root, participant_root=lake_root, dry_run=False, db_path=db_path)

    rows_after = conn.execute("SELECT * FROM dealer_exposure_daily ORDER BY date").fetchall()
    assert len(rows_after) == 2
    assert rows_after[1]["dealer"] == "non_client"
    assert rows_after[1]["net_vex"] is not None and rows_after[1]["net_cex"] is not None
    conn.close()


def test_macro_lake_adds_uscpi_10y_breakeven_and_yfinance_ice_dxy(tmp_path):
    assert ml.SERIES["USCPI"] == "CPIAUCSL"
    assert ml.SERIES["US10Y_BE"] == "T10YIE"
    assert ml.YFINANCE_SERIES["DXY_ICE"] == "DX-Y.NYB"

    fred_payload = json.dumps({
        "observations": [
            {"date": "2026-09-01", "value": "315.4"},
            {"date": "2026-10-01", "value": "316.1"},
        ]
    }).encode()

    r_cpi = ml.ingest("USCPI", api_key="TEST", fetch_bytes_fn=lambda u: fred_payload, lake_dir=tmp_path)
    r_be = ml.ingest("US10Y_BE", api_key="TEST", fetch_bytes_fn=lambda u: fred_payload, lake_dir=tmp_path)
    assert r_cpi["rows_added"] == 2 and r_be["rows_added"] == 2

    # True ICE DXY (DX-Y.NYB) via injectable yfinance seam
    yf_data = {"2026-10-28": 104.12, "2026-10-29": 104.35}
    r_dxy = ml.ingest_yfinance("DXY_ICE", yf_fetch_fn=lambda t: yf_data, lake_dir=tmp_path)
    assert r_dxy["ticker"] == "DX-Y.NYB" and r_dxy["rows_added"] == 2
    assert ml.read_series("DXY_ICE", lake_dir=tmp_path) == [("2026-10-28", 104.12), ("2026-10-29", 104.35)]


def test_gex_steering_steer_is_untouched():
    # VEX/CEX remain strictly shadow data; gex_steering.steer ignores them
    gex_read = {
        "zero_gamma": 23_400.0,
        "regime_at_spot": "negative",
        "day": "2026-10-29",
        "net_vex": -999_999_999.0,
        "net_cex": 999_999_999.0,
    }
    res = gs.steer(
        "NIFTY 50",
        "bull_call_spread",
        23_000.0,
        analysis={"closes": [22_800.0, 23_000.0]},
        gex_fn=lambda _u: gex_read,
    )
    assert res["allowed"] is True
    assert res["zone"] == "short_gamma"
    assert res["wanted"] == "bull_call_spread"
