"""
Hermetic test suite for Phase 3+ Multi-Seat Tenancy (`src/tenancy.py`),
Unified 'Proven on Paper' Standard & Survivorship-Bias Guard (`src/validation/stat_gates.py`,
`src/analysis/auto_discovery.py`), Golden Corporate Actions (`src/ingestion/corporate_actions.py`),
MFE/MAE Open-Book Dry-Run (`src/calibration/mfe_mae_analyzer.py`), and Vector 1
Wings-First Multi-Leg Execution (`next_gen_engine/execution_algo.py`, `src/execution/paper_venue.py`).
"""
from __future__ import annotations

import copy
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from next_gen_engine import execution_algo as ex_algo
from src import oms, strategy_router as sr, tenancy
from src.analysis import auto_discovery as ad
from src.calibration import mfe_mae_analyzer as mfe
from src.execution import paper_venue as pv
from src.ingestion import corporate_actions as ca
from src.strategy import StrategyConstructor
from src.validation import stat_gates as sg


# ------------------------------------------------------------------ Vector 6: Tenancy & Role Permissions

def test_tenancy_id_validation_and_isolated_sqlite_paths(tmp_path: Path):
    assert tenancy.validate_tenant_id(None) == "owner"
    assert tenancy.validate_tenant_id("") == "owner"
    assert tenancy.validate_tenant_id("PRIMARY") == "owner"
    assert tenancy.validate_tenant_id("seat_alpha-1") == "seat_alpha-1"

    for bad in ("../etc", "seat/a", "seat\\b", "-bad", "a" * 35, "seat space"):
        with pytest.raises(ValueError, match="invalid tenant_id"):
            tenancy.validate_tenant_id(bad)

    owner_path = tenancy.tenant_db_path("owner", base_dir=tmp_path)
    seat_a_path = tenancy.tenant_db_path("seat_a", base_dir=tmp_path)
    seat_b_path = tenancy.tenant_db_path("seat_b", base_dir=tmp_path)

    assert owner_path == tmp_path.resolve() / "brain_map.db"
    assert seat_a_path == tmp_path.resolve() / "tenants" / "seat_a" / "brain_map.db"
    assert seat_b_path == tmp_path.resolve() / "tenants" / "seat_b" / "brain_map.db"


def test_tenancy_sqlite_isolation_and_cross_tenant_attach_firewall(tmp_path: Path):
    conn_owner = tenancy.connect_tenant("owner", mode="rw", base_dir=tmp_path, role="owner")
    conn_a = tenancy.connect_tenant("seat_a", mode="rw", base_dir=tmp_path, role="operator")
    conn_b = tenancy.connect_tenant("seat_b", mode="rw", base_dir=tmp_path, role="analyst")
    try:
        for c, label in ((conn_owner, "owner_only"), (conn_a, "seat_a_only"), (conn_b, "seat_b_only")):
            c.execute("CREATE TABLE seat_marker (label TEXT NOT NULL)")
            c.execute("INSERT INTO seat_marker (label) VALUES (?)", (label,))
            c.commit()

        assert [r[0] for r in conn_owner.execute("SELECT label FROM seat_marker")] == ["owner_only"]
        assert [r[0] for r in conn_a.execute("SELECT label FROM seat_marker")] == ["seat_a_only"]
        assert [r[0] for r in conn_b.execute("SELECT label FROM seat_marker")] == ["seat_b_only"]

        # Cross-tenant ATTACH DATABASE is blocked by the SQLite authorizer firewall
        seat_b_file = tenancy.tenant_db_path("seat_b", base_dir=tmp_path)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            conn_a.execute(f"ATTACH DATABASE '{seat_b_file}' AS other_tenant")

        # Viewer seat is strictly read-only
        with pytest.raises(PermissionError, match="viewer seat role is restricted"):
            tenancy.connect_tenant("seat_a", mode="rw", base_dir=tmp_path, role="viewer")

        conn_viewer = tenancy.connect_tenant("seat_a", mode="ro", base_dir=tmp_path, role="viewer")
        try:
            assert conn_viewer.execute("SELECT label FROM seat_marker").fetchone()[0] == "seat_a_only"
            with pytest.raises(sqlite3.OperationalError):
                conn_viewer.execute("INSERT INTO seat_marker (label) VALUES ('illegal')")
        finally:
            conn_viewer.close()
    finally:
        conn_owner.close()
        conn_a.close()
        conn_b.close()

    # Pytest muzzle blocks opening the real repo data/ directory without tmp_path
    with pytest.raises(RuntimeError, match="muzzled under pytest"):
        tenancy.connect_tenant("owner", mode="rw")


def test_tenancy_role_permissions_and_node_topology_enforcement():
    vm_env = {"ALPHA_NODE_ROLE": "execution"}
    node_env = {"ALPHA_NODE_ROLE": "analytics"}

    # Owner on execution VM can approve paper trades, trip/clear halts, and administer tenants
    assert tenancy.check_seat_permission(
        "owner", "approve_paper", tenant_id="owner", env=vm_env, hostname="alpha-vm"
    )["allowed"] is True
    assert tenancy.check_seat_permission(
        "owner", "halt_clear", tenant_id="owner", env=vm_env, hostname="alpha-vm"
    )["allowed"] is True
    assert tenancy.check_seat_permission(
        "owner", "read", tenant_id="owner", target_tenant_id="seat_a", env=vm_env, hostname="alpha-vm"
    )["allowed"] is True

    # Operator can approve paper trades and trip emergency halt, but cannot clear halt or inject capital
    assert tenancy.check_seat_permission(
        "operator", "approve_paper", tenant_id="seat_a", env=vm_env, hostname="alpha-vm"
    )["allowed"] is True
    assert tenancy.check_seat_permission(
        "operator", "halt_trip", tenant_id="seat_a", env=vm_env, hostname="alpha-vm"
    )["allowed"] is True
    res_clear = tenancy.check_seat_permission(
        "operator", "halt_clear", tenant_id="seat_a", env=vm_env, hostname="alpha-vm"
    )
    assert res_clear["allowed"] is False and "role_permission_denied" in res_clear["reason"]

    # Non-owner seat cannot cross tenant boundary
    res_cross = tenancy.check_seat_permission(
        "operator", "read", tenant_id="seat_a", target_tenant_id="seat_b", env=vm_env, hostname="alpha-vm"
    )
    assert res_cross["allowed"] is False and "cross_tenant_denied" in res_cross["reason"]

    # Topology (#146): Mini PC analytics node refuses execution mutations even for owner
    res_node_exec = tenancy.check_seat_permission(
        "owner", "approve_paper", tenant_id="owner", env=node_env, hostname="minipc1"
    )
    assert res_node_exec["allowed"] is False and "topology_execution_refused" in res_node_exec["reason"]

    # Topology (#149): Execution VM refuses LLM extraction unless on analytics node
    res_vm_llm = tenancy.check_seat_permission(
        "analyst", "llm_extract", tenant_id="seat_a", env=vm_env, hostname="alpha-vm"
    )
    assert res_vm_llm["allowed"] is False and "topology_llm_refused" in res_vm_llm["reason"]
    assert tenancy.check_seat_permission(
        "analyst", "llm_extract", tenant_id="seat_a", env=node_env, hostname="minipc1"
    )["allowed"] is True


# ------------------------------------------------------------------ Unified Paper-Proof Standard & Survivorship Guard

def test_unified_paper_proof_standard_and_backfill_survivorship_metadata(tmp_path: Path):
    # Passes when all 4 criteria hold: forward sample, pre-registered metric, n >= min_n, beats null + placebo
    ok = sg.evaluate_paper_proof_standard(
        vector_name="Vector 3 GraphRAG",
        sample_source="forward",
        metric_pre_registered=True,
        n_samples=64,
        min_n=60,
        wilson_lb=0.58,
        null_bound=0.50,
        placebo_p_value=0.012,
        max_placebo_alpha=0.05,
    )
    assert ok["proven_on_paper"] is True
    assert all(ok["checks"].values())

    # Rejects backfilled sample even with huge n and strong stats (survivorship/lookahead guard)
    backfilled = sg.evaluate_paper_proof_standard(
        vector_name="Vector 4 Cycle Hunter",
        sample_source="backfilled",
        metric_pre_registered=True,
        n_samples=500,
        min_n=60,
        wilson_lb=0.72,
        null_bound=0.50,
        placebo_p_value=0.001,
    )
    assert backfilled["proven_on_paper"] is False
    assert backfilled["checks"]["forward_only"] is False
    assert any("not 'forward'" in r for r in backfilled["reasons"])

    # Rejects un-preregistered metric, insufficient n, or weak placebo p-value
    failed_gates = sg.evaluate_paper_proof_standard(
        vector_name="Vector 5 Playbooks",
        sample_source="forward",
        metric_pre_registered=False,
        n_samples=18,
        min_n=60,
        wilson_lb=0.49,
        null_bound=0.50,
        placebo_p_value=0.14,
    )
    assert failed_gates["proven_on_paper"] is False
    assert len(failed_gates["reasons"]) == 4

    # Historical backfill metadata explicitly flags survivorship bias
    disc = ad.discover(lake_dir=tmp_path, dry_run=True)
    prov = disc["provenance"]
    assert prov["survivorship_bias_warning"] is True
    assert prov["evidence_status"] == "hypothesis_only_not_forward_proof"
    assert prov["forward_ledger_untouched"] is True


# ------------------------------------------------------------------ Golden Corporate Actions (Split + Bonus)

def test_corporate_actions_golden_irctc_split_and_nestleind_split_plus_bonus():
    # Golden test 1: IRCTC 1:5 split on 2021-10-28 (prev_close 4130.15 -> open 817.0)
    irctc_bars = [
        {"session": "2021-10-27", "date": "2021-10-27", "open": 4200.0, "high": 4250.0, "low": 4100.0, "close": 4130.15, "prev_close": 4180.0, "volume": 5_400_000.0},
        {"session": "2021-10-28", "date": "2021-10-28", "open": 817.0, "high": 855.0, "low": 805.0, "close": 845.70, "prev_close": 4130.15, "volume": 59_300_000.0},
    ]
    irctc_adj = ca.adjusted("IRCTC", irctc_bars)
    assert irctc_adj[0]["adj_factor"] == 5.0
    assert irctc_adj[0]["close"] == round(4130.15 / 5.0, 4)
    assert irctc_adj[0]["raw_close"] == 4130.15
    assert irctc_adj[0]["volume"] == 27_000_000.0
    assert irctc_adj[1]["adj_factor"] == 1.0
    assert irctc_adj[1]["close"] == 845.70

    # Golden test 2: NESTLEIND 1:10 split (2024-01-05) + 1:1 bonus (2025-08-08, ratio 2.0)
    nestle_bars = [
        {"session": "2024-01-04", "date": "2024-01-04", "open": 27000.0, "high": 27200.0, "low": 26900.0, "close": 27116.40, "prev_close": 26950.0, "volume": 132_000.0},
        {"session": "2024-01-05", "date": "2024-01-05", "open": 2754.0, "high": 2780.0, "low": 2660.0, "close": 2668.0, "prev_close": 27116.40, "volume": 2_580_000.0},
        {"session": "2025-08-07", "date": "2025-08-07", "open": 2440.0, "high": 2460.0, "low": 2420.0, "close": 2450.0, "prev_close": 2435.0, "volume": 800_000.0},
        {"session": "2025-08-08", "date": "2025-08-08", "open": 1230.0, "high": 1245.0, "low": 1215.0, "close": 1228.0, "prev_close": 2450.0, "volume": 1_960_000.0},
    ]
    nestle_adj = ca.adjusted("NESTLEIND", nestle_bars)
    # Pre-2024-01-05 scales by cumulative 10 * 2 = 20x
    assert nestle_adj[0]["adj_factor"] == 20.0
    assert nestle_adj[0]["close"] == round(27116.40 / 20.0, 4)
    # Between 2024-01-05 and 2025-08-08 scales by 2x (the later 1:1 bonus)
    assert nestle_adj[1]["adj_factor"] == 2.0
    assert nestle_adj[1]["close"] == 1334.0
    assert nestle_adj[2]["adj_factor"] == 2.0
    assert nestle_adj[2]["close"] == 1225.0
    # On/after latest ex_date (2025-08-08) is untouched raw exchange print (factor 1.0)
    assert nestle_adj[3]["adj_factor"] == 1.0
    assert nestle_adj[3]["close"] == 1228.0


# ------------------------------------------------------------------ MFE/MAE Open-Book Dry-Run Evaluator

def test_mfe_mae_dry_run_open_book_and_court_gate():
    records = [
        {"strategy": "bull_call_spread", "mfe_pct": 3.2, "mae_pct": 0.8, "pnl": 4500.0}
        for _ in range(18)
    ] + [
        {"strategy": "bull_call_spread", "mfe_pct": 0.6, "mae_pct": 2.1, "pnl": -3200.0}
        for _ in range(7)
    ]
    summary = mfe.summarize(records, min_samples=20)
    open_book = [
        {
            "short_id": "op1",
            "ticker": "NIFTY 50",
            "decision": "approved",
            "outcome": None,
            "spread": {"strategy": "bull_call_spread", "entry_spot": 25000.0},
        },
        {
            "short_id": "op2_closed",
            "ticker": "NIFTY 50",
            "decision": "approved",
            "outcome": {"exit_date": "2026-10-20", "pnl_rs": 1200.0},
            "spread": {"strategy": "bull_call_spread", "entry_spot": 24800.0},
        },
    ]
    snapshot_before = copy.deepcopy(open_book)

    # Without Court verdict -> DRY_RUN_ONLY_AWAITING_COURT, deploy_ready=False
    dry_unapproved = mfe.dry_run_open_book(open_book, summary, court_verdict=None)
    assert open_book == snapshot_before
    assert dry_unapproved["n_open_evaluated"] == 1
    assert dry_unapproved["skipped_closed_or_rejected"] == 1
    assert dry_unapproved["deploy_ready"] is False
    assert dry_unapproved["live_override"] is False
    assert dry_unapproved["status"] == "DRY_RUN_ONLY_AWAITING_COURT"
    pos = dry_unapproved["positions"][0]
    assert pos["apex_source"] == "cluster:bull_call_spread"
    assert pos["apex_candidate"]["status"] == "ok"
    assert pos["apex_candidate"]["implied_tp_spot"] > 25000.0
    assert pos["apex_candidate"]["implied_sl_spot"] < 25000.0

    # With passing Department-5 Court verdict -> COURT_APPROVED_ADVISORY, live_override still False
    court_ok = sg.evaluate_paper_proof_standard(
        vector_name="Vector 4 MFE/MAE Apex",
        sample_source="forward",
        metric_pre_registered=True,
        n_samples=60,
        min_n=60,
        wilson_lb=0.61,
        null_bound=0.50,
        placebo_p_value=0.01,
    )
    dry_approved = mfe.dry_run_open_book(open_book, summary, court_verdict=court_ok)
    assert dry_approved["deploy_ready"] is True
    assert dry_approved["live_override"] is False
    assert dry_approved["status"] == "COURT_APPROVED_ADVISORY"


# ------------------------------------------------------------------ Vector 1: Wings-First Multi-Leg Chase & OMS Guard

def test_wings_first_multi_leg_chase_and_paper_venue_unhedged_wing_guard():
    legs = [
        {"leg_id": "short_call", "side": "sell", "strike": 25200},
        {"leg_id": "long_call", "side": "buy", "strike": 25000},
    ]
    # Case 1: Protective BUY wing fills -> short SELL wing is worked and fills
    quotes_ok = {
        "long_call": [{"top_bid": 100.0, "top_ask": 101.0}, {"top_bid": 100.0, "top_ask": 100.5}],
        "short_call": [{"top_bid": 40.0, "top_ask": 41.0}, {"top_bid": 40.5, "top_ask": 41.0}],
    }
    res_ok = ex_algo.work_multi_leg_chase(legs, quotes_ok, steps=3)
    assert res_ok["wings_first_verified"] is True
    assert [l["side"] for l in res_ok["legs"]] == ["buy", "sell"]
    assert res_ok["all_filled"] is True
    assert res_ok["aborted_unhedged_wing"] is False

    # Case 2: Protective BUY wing misses chase window -> short SELL wing is aborted, never submitted
    quotes_wing_miss = {
        "long_call": [{"top_bid": 100.0, "top_ask": 101.0}, {"top_bid": 102.0, "top_ask": 103.0}],
        "short_call": [{"top_bid": 40.0, "top_ask": 41.0}, {"top_bid": 40.8, "top_ask": 41.0}],
    }
    res_abort = ex_algo.work_multi_leg_chase(legs, quotes_wing_miss, steps=2)
    assert res_abort["all_filled"] is False
    assert res_abort["aborted_unhedged_wing"] is True
    assert res_abort["legs"][0]["side"] == "buy" and res_abort["legs"][0]["filled"] is False
    assert res_abort["legs"][1]["side"] == "sell" and res_abort["legs"][1].get("aborted") is True

    # Case 3: Paper Venue enforce_wings_first cancels short SELL leg if protective BUY leg is rejected
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    oms.ensure_schema(conn)
    sc = StrategyConstructor(vix=13.0, lot_size=65)
    spread = dict(sc.construct_bull_call_spread(25000, 25200, 100.0, 30.0), lots=1, expiry="2026-10-30")
    # Invalidate the protective BUY leg's limit price
    for leg in spread["legs"]:
        if str(leg["side"]).upper() == "BUY":
            leg["premium"] = None
    proposal = {"ticker": "NIFTY 50", "short_id": "wf00001", "signal": "t", "spread": spread}
    tid = sr.issue(conn, proposal)["ticket_id"]
    view_before = oms.ticket_view(conn, tid)
    assert pv.verify_wings_first_order(view_before["legs"]) is True

    sweep_res = pv.sweep(conn, today=date(2026, 10, 28), slippage_fn=lambda u: 0.0, stamp=False, enforce_wings_first=True)
    assert sweep_res["rejected"] == 1
    assert sweep_res["cancelled_unhedged"] == 1
    assert sweep_res["filled"] == 0
    view_after = oms.ticket_view(conn, tid)
    assert view_after["status"] == oms.REJECTED
    states_by_side = {l["side"]: l["state"] for l in view_after["legs"]}
    assert states_by_side == {"BUY": oms.REJECTED, "SELL": oms.CANCELLED}

