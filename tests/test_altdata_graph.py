"""Hermetic unit tests for the AltData GraphRAG Injector (`src/analysis/altdata_graph.py` and `scripts/update_graph_altdata.py`)."""

from __future__ import annotations

import json
import math

import pytest

import scripts.update_graph_altdata as update_script
from src import brain_map
from src.analysis.altdata_graph import (
    AGRI_DEPENDENCY_MAP,
    ALTDATA_EXTRACTOR_TAG,
    ALTDATA_SOURCE_TAG,
    AltDataGraphInjector,
    compute_7d_velocity,
    compute_gem_edge_weight,
    compute_input_cost_pressure_weight,
)
from src.graph_engine import GraphEngine

GEM_CSV_CONTENT = """date,bid_number,winning_bidder,ticker,bid_value,item_category,govt_entity
2026-11-02,GEM/2026/B/9001122,TATA MOTORS LIMITED,TATAMOTORS.NS,425000000.00,Electric Buses - 12 Meter AC,MORTH_STATE_TRANSPORT
2026-11-02,GEM/2026/B/9001188,BHARAT ELECTRONICS LIMITED,BEL.NS,18500000.00,Radar Signal Processing Units,
"""

AGMARKNET_CSV_CONTENT = """date,commodity,state,mandi,min_price_quintal,max_price_quintal,modal_price_quintal,modal_price_kg
2026-10-26,Wheat,Madhya Pradesh,Indore Mandi,2400.00,2600.00,2500.00,25.00
2026-11-02,Wheat,Madhya Pradesh,Indore Mandi,2650.00,2850.00,2750.00,27.50
2026-10-26,Sugar,Maharashtra,Kolhapur APMC,3900.00,4100.00,4000.00,40.00
2026-11-02,Sugar,Maharashtra,Kolhapur APMC,3700.00,3900.00,3800.00,38.00
"""

VAHAN_CSV_CONTENT = """date,maker,ticker,fuel_type,registrations
2026-10-26,Tata,TATAMOTORS.NS,EV,500
2026-10-26,Tata,TATAMOTORS.NS,ICE,1500
2026-11-02,Tata,TATAMOTORS.NS,EV,650
2026-11-02,Tata,TATAMOTORS.NS,ICE,1750
2026-10-26,Maruti,MARUTI.NS,EV,0
2026-10-26,Maruti,MARUTI.NS,ICE,5000
2026-11-02,Maruti,MARUTI.NS,EV,0
2026-11-02,Maruti,MARUTI.NS,ICE,4500
"""


def test_gem_wiring_creates_won_contract_edges_with_log_bid_weight():
    conn = brain_map.connect(":memory:")
    injector = AltDataGraphInjector(conn=conn)

    rows = [
        {
            "date": "2026-11-02",
            "bid_number": "GEM/2026/B/101",
            "winning_bidder": "TATA MOTORS LIMITED",
            "ticker": "TATAMOTORS.NS",
            "bid_value": "425000000.00",
            "item_category": "Electric Buses - 12 Meter AC",
            "govt_entity": "MORTH_STATE_TRANSPORT",
        },
        {
            "date": "2026-11-02",
            "bid_number": "GEM/2026/B/102",
            "winning_bidder": "BHARAT ELECTRONICS LIMITED",
            "ticker": "",
            "bid_value": "18500000.00",
            "item_category": "Radar Signal Processing Units",
        },
    ]

    res = injector.inject_gem_tenders(rows=rows)
    assert res["edges_upserted"] == 2

    db_edges = {
        (r["source_node"], r["relation"], r["target_node"]): r
        for r in conn.execute("SELECT * FROM graph_edges").fetchall()
    }

    # 1. Explicit govt_entity: TATAMOTORS.NS --WON_CONTRACT--> MORTH_STATE_TRANSPORT
    tata_edge = db_edges[("TATAMOTORS.NS", "WON_CONTRACT", "MORTH_STATE_TRANSPORT")]
    expected_tata_weight = math.log(425_000_000.0)
    assert compute_gem_edge_weight(425_000_000.0) == pytest.approx(expected_tata_weight)
    assert float(tata_edge["weight"]) == pytest.approx(expected_tata_weight)
    assert int(tata_edge["polarity"]) == 1
    assert tata_edge["source"] == ALTDATA_SOURCE_TAG
    assert tata_edge["extractor"] == ALTDATA_EXTRACTOR_TAG

    # 2. Inferred govt_entity from Radar category + matched bidder ticker: BEL.NS --WON_CONTRACT--> MINISTRY_OF_DEFENCE
    bel_edge = db_edges[("BEL.NS", "WON_CONTRACT", "MINISTRY_OF_DEFENCE")]
    expected_bel_weight = math.log(18_500_000.0)
    assert float(bel_edge["weight"]) == pytest.approx(expected_bel_weight)
    conn.close()


def test_agmarknet_wiring_calculates_7d_velocity_and_inverse_cost_pressure_edges():
    conn = brain_map.connect(":memory:")
    assert "ITC.NS" in AGRI_DEPENDENCY_MAP["Wheat"]
    assert "BRITANNIA.NS" in AGRI_DEPENDENCY_MAP["Wheat"]
    assert "NESTLEIND.NS" in AGRI_DEPENDENCY_MAP["Sugar"]

    custom_deps = {
        "Wheat": ["ITC.NS", "BRITANNIA.NS"],
        "Sugar": ["ITC.NS", "BRITANNIA.NS", "NESTLEIND.NS"],
    }
    injector = AltDataGraphInjector(conn=conn, dependency_map=custom_deps)

    # Wheat rises +10% over 7 days (2500 -> 2750): velocity_7d = +0.10 -> edge weight = -0.10 (negative pressure)
    # Sugar falls -5% over 7 days (4000 -> 3800): velocity_7d = -0.05 -> edge weight = +0.05 (cost relief)
    rows = [
        {"date": "2026-10-26", "commodity": "Wheat", "modal_price_quintal": "2500.00"},
        {"date": "2026-11-02", "commodity": "Wheat", "modal_price_quintal": "2750.00"},
        {"date": "2026-10-26", "commodity": "Sugar", "modal_price_quintal": "4000.00"},
        {"date": "2026-11-02", "commodity": "Sugar", "modal_price_quintal": "3800.00"},
    ]

    res = injector.inject_agmarknet_mandi(rows=rows)
    assert res["edges_upserted"] == 5  # 2 Wheat equities + 3 Sugar equities

    db_edges = {
        (r["source_node"], r["relation"], r["target_node"]): r
        for r in conn.execute(
            "SELECT * FROM graph_edges WHERE relation = 'INPUT_COST_PRESSURE'"
        ).fetchall()
    }

    wheat_itc = db_edges[("Wheat", "INPUT_COST_PRESSURE", "ITC.NS")]
    assert float(wheat_itc["weight"]) == pytest.approx(-0.10)
    assert int(wheat_itc["polarity"]) == -1
    assert compute_input_cost_pressure_weight(0.10) == pytest.approx(-0.10)

    wheat_brit = db_edges[("Wheat", "INPUT_COST_PRESSURE", "BRITANNIA.NS")]
    assert float(wheat_brit["weight"]) == pytest.approx(-0.10)

    sugar_nestle = db_edges[("Sugar", "INPUT_COST_PRESSURE", "NESTLEIND.NS")]
    assert float(sugar_nestle["weight"]) == pytest.approx(0.05)
    assert int(sugar_nestle["polarity"]) == 1

    # Verify momentum modifiers table entries
    mods = {
        (r["ticker"], r["source_entity"]): r
        for r in conn.execute(
            "SELECT * FROM altdata_momentum_modifiers WHERE signal_type = 'INPUT_COST_PRESSURE'"
        ).fetchall()
    }
    assert float(mods[("ITC.NS", "Wheat")]["velocity_7d"]) == pytest.approx(0.10)
    assert float(mods[("ITC.NS", "Wheat")]["edge_weight"]) == pytest.approx(-0.10)
    assert float(mods[("NESTLEIND.NS", "Sugar")]["velocity_7d"]) == pytest.approx(-0.05)
    assert float(mods[("NESTLEIND.NS", "Sugar")]["edge_weight"]) == pytest.approx(0.05)
    conn.close()


def test_vahan_wiring_calculates_7d_registration_velocity_per_ticker():
    conn = brain_map.connect(":memory:")
    injector = AltDataGraphInjector(conn=conn)

    # TATAMOTORS.NS: 2026-10-26 total = 500 EV + 1500 ICE = 2000; 2026-11-02 total = 650 EV + 1750 ICE = 2400 (+20% velocity)
    # MARUTI.NS:     2026-10-26 total = 5000; 2026-11-02 total = 4500 (-10% velocity)
    rows = [
        {"date": "2026-10-26", "maker": "Tata", "ticker": "TATAMOTORS.NS", "fuel_type": "EV", "registrations": "500"},
        {"date": "2026-10-26", "maker": "Tata", "ticker": "TATAMOTORS.NS", "fuel_type": "ICE", "registrations": "1500"},
        {"date": "2026-11-02", "maker": "Tata", "ticker": "TATAMOTORS.NS", "fuel_type": "EV", "registrations": "650"},
        {"date": "2026-11-02", "maker": "Tata", "ticker": "TATAMOTORS.NS", "fuel_type": "ICE", "registrations": "1750"},
        {"date": "2026-10-26", "maker": "Maruti", "ticker": "MARUTI.NS", "fuel_type": "ICE", "registrations": "5000"},
        {"date": "2026-11-02", "maker": "Maruti", "ticker": "MARUTI.NS", "fuel_type": "ICE", "registrations": "4500"},
    ]

    res = injector.inject_vahan_registrations(rows=rows)
    assert res["edges_upserted"] == 2

    db_edges = {
        r["source_node"]: r
        for r in conn.execute(
            "SELECT * FROM graph_edges WHERE relation = 'VEHICLE_SALES_MOMENTUM'"
        ).fetchall()
    }
    assert float(db_edges["TATAMOTORS.NS"]["weight"]) == pytest.approx(0.20)
    assert int(db_edges["TATAMOTORS.NS"]["polarity"]) == 1
    tata_ctx = json.loads(db_edges["TATAMOTORS.NS"]["context"])
    assert tata_ctx["ev_share_pct"] == pytest.approx(27.08)

    assert float(db_edges["MARUTI.NS"]["weight"]) == pytest.approx(-0.10)
    assert int(db_edges["MARUTI.NS"]["polarity"]) == -1

    mods = {
        r["ticker"]: r
        for r in conn.execute(
            "SELECT * FROM altdata_momentum_modifiers WHERE signal_type = 'VEHICLE_SALES_MOMENTUM'"
        ).fetchall()
    }
    assert float(mods["TATAMOTORS.NS"]["velocity_7d"]) == pytest.approx(0.20)
    assert float(mods["TATAMOTORS.NS"]["momentum_modifier"]) == pytest.approx(0.20)
    assert float(mods["MARUTI.NS"]["velocity_7d"]) == pytest.approx(-0.10)
    assert float(mods["MARUTI.NS"]["momentum_modifier"]) == pytest.approx(-0.10)
    conn.close()


def test_update_graph_altdata_wrapper_end_to_end_and_idempotent_upserts(tmp_path):
    lake_dir = tmp_path / "alt_data"
    lake_dir.mkdir(parents=True, exist_ok=True)
    (lake_dir / "gem_tenders.csv").write_text(GEM_CSV_CONTENT, encoding="utf-8")
    (lake_dir / "agmarknet_mandi.csv").write_text(AGMARKNET_CSV_CONTENT, encoding="utf-8")
    (lake_dir / "vahan_registrations.csv").write_text(VAHAN_CSV_CONTENT, encoding="utf-8")

    conn = brain_map.connect(":memory:")
    first = update_script.run_update(conn=conn, lake_dir=lake_dir)
    assert first["ok"] is True
    # 2 GeM + 9 Agmarknet (4 Wheat + 5 Sugar) + 2 Vahan = 13 edges
    assert first["total_edges_upserted"] == 13

    edge_count_1 = conn.execute("SELECT COUNT(*) AS n FROM graph_edges").fetchone()["n"]
    mod_count_1 = conn.execute("SELECT COUNT(*) AS n FROM altdata_momentum_modifiers").fetchone()["n"]
    assert edge_count_1 == 13
    assert mod_count_1 == 13

    # Re-running against the exact same CSVs + DB is 100% idempotent (no duplicate rows)
    second = update_script.run_update(conn=conn, lake_dir=lake_dir)
    assert second["ok"] is True
    edge_count_2 = conn.execute("SELECT COUNT(*) AS n FROM graph_edges").fetchone()["n"]
    mod_count_2 = conn.execute("SELECT COUNT(*) AS n FROM altdata_momentum_modifiers").fetchone()["n"]
    assert edge_count_2 == 13
    assert mod_count_2 == 13

    # Verify GraphEngine loads the injected edges into memory and traverses them
    engine = GraphEngine(conn=conn)
    tata_ctx = engine.get_relevant_context("TATAMOTORS.NS", max_hops=1)
    relations = {e["relation"] for e in tata_ctx}
    assert "WON_CONTRACT" in relations
    assert "VEHICLE_SALES_MOMENTUM" in relations

    wheat_ctx = engine.get_relevant_context("Wheat", max_hops=1)
    wheat_targets = {e["target"] for e in wheat_ctx if e["relation"] == "INPUT_COST_PRESSURE"}
    assert {"ITC.NS", "BRITANNIA.NS", "NESTLEIND.NS", "HINDUNILVR.NS"}.issubset(wheat_targets)

    # Also test compute_7d_velocity helper edge cases
    assert compute_7d_velocity([("2026-11-02", 100.0)]) is None
    conn.close()
