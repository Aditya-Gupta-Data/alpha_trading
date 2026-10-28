"""Hermetic tests for Phase 2.5 / Phase 3 Prep:
- GraphRAG & provenance lane in `src/graph_viz.py`
- Read-only MCP tools `graphrag_propagation` and `bayesian_win_posterior` in `src/brain_mcp.py`
- M4 5-bucket ceiling & precedence allocator + halted-capital borrowing firewall in `src/buckets.py`
- M1 migration parity & idempotent apply in `scripts/migrate_m1_buckets.py`
"""
from pathlib import Path

import pytest

from scripts import migrate_m1_buckets
from src import brain_map, brain_mcp, buckets, graph_engine, graph_viz, portfolio_manager as pm
from src.knowledge_graph import graphrag


def test_graph_viz_includes_graphrag_edges_and_node_types():
    conn = brain_map.connect(":memory:")
    graphrag.ensure_schema(conn)
    src_macro = {"node_id": "CRUDE_UP", "node_type": "macro_factor", "in_universe": 0}
    tgt_ongc = {"node_id": "ONGC", "node_type": "ticker", "in_universe": 1}
    tgt_indigo = {"node_id": "INDIGO", "node_type": "ticker", "in_universe": 1}
    graphrag.upsert_graphrag_edge(
        conn, src_macro, "benefits_from", tgt_ongc,
        edge_class="causal", polarity=1, strength=0.82,
        content_hash="h_crude_ongc", quote="ONGC benefits from crude spike",
    )
    graphrag.upsert_graphrag_edge(
        conn, src_macro, "hurts", tgt_indigo,
        edge_class="cost", polarity=-1, strength=0.79,
        content_hash="h_crude_indigo", quote="Crude hurts aviation margins",
    )
    graph_engine.add_edge(
        conn, "ONGC", "INDICATES", "INDIGO",
        confidence_score=0.65, context="sector pair",
    )

    payload = graph_viz.build_graph_json(conn)
    assert payload["stats"]["graphrag"] == 2
    assert payload["stats"]["outcome_derived"] == 1
    assert payload["stats"]["edges_active"] == 3

    by_id = {n["id"]: n for n in payload["nodes"]}
    assert by_id["CRUDE_UP"]["node_type"] == "macro_factor"
    assert by_id["CRUDE_UP"]["in_universe"] is False
    assert by_id["ONGC"]["node_type"] == "ticker"
    assert by_id["ONGC"]["in_universe"] is True

    html = graph_viz.render_html(payload)
    assert "--graphrag" in html
    assert 'id="f-graphrag"' in html
    conn.close()


def _mcp_call(src, tool: str, arguments: dict):
    import json
    resp = brain_mcp.handle_message({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }, src)
    assert resp["result"]["isError"] is False, resp
    return json.loads(resp["result"]["content"][0]["text"])


def test_brain_mcp_graphrag_propagation_and_bayesian_win_posterior(tmp_path: Path):
    (tmp_path / "data").mkdir(exist_ok=True)
    db_path = tmp_path / "data" / "brain_map.db"

    conn = brain_map.connect(db_path)
    graphrag.ensure_schema(conn)
    src_node = {"node_id": "RATE_CUT", "node_type": "macro_factor", "in_universe": 0}
    tgt_node = {"node_id": "HDFCBANK", "node_type": "ticker", "in_universe": 1}
    graphrag.upsert_graphrag_edge(
        conn, src_node, "tailwinds", tgt_node,
        edge_class="causal", polarity=1, strength=0.85,
        content_hash="h_rate_hdfc", quote="Rate cut tailwinds HDFCBANK",
    )
    for i, r_mult in enumerate((1.4, 0.9, -0.5, 1.1, 0.8), start=1):
        brain_map.record_outcome(
            conn, f"opt{i:05d}", f"2026-10-0{i}", "HDFCBANK",
            archetype="bull_call_spread", r_multiple=r_mult,
        )
    conn.close()

    src = brain_mcp.Sources(root=tmp_path)
    prop = _mcp_call(src, "graphrag_propagation", {
        "seed": "RATE_CUT",
        "polarity": 1,
    })
    assert prop["available"] is True
    assert prop["seed"] == "RATE_CUT"
    assert len(prop["activated"]) == 1
    assert prop["activated"][0]["node_id"] == "HDFCBANK"
    assert prop["activated"][0]["energy"] > 0

    post = _mcp_call(src, "bayesian_win_posterior", {
        "archetype": "bull_call_spread",
        "ticker": "HDFCBANK",
    })
    assert post["available"] is True
    p_inner = post["posterior"]
    assert p_inner["raw_wins"] == 4
    assert p_inner["raw_losses"] == 1
    assert p_inner["raw_n"] == 5
    assert 0.55 < p_inner["posterior_mean"] < 0.85
    assert p_inner["posterior_std"] > 0
    assert 0.0 < p_inner["wilson_lb"] < p_inner["posterior_mean"]


def test_m4_bucket_ceilings_capped_at_100_pct_by_precedence():
    # Sum of B1..B5 ceilings is 25 + 20 + 30 + 25 + 15 = 115%.
    # When all 5 buckets demand their ceiling on Rs.10,00,000, total allocation
    # must never exceed Rs.10,00,000 (100.0%), clipped deterministically by BUCKET_PRECEDENCE.
    assert sum(buckets.BUCKET_CEILINGS_PCT.values()) == 115.0

    res = buckets.allocate_bucket_ceilings(1_000_000.0)
    assert res["total_allocated_rs"] == 1_000_000.0
    assert res["total_allocated_pct"] == 100.0
    assert res["allocations"]["WEALTH_LOCK"] == 250_000.0   # 25%
    assert res["allocations"]["DARLINGS"] == 300_000.0      # 30%
    assert res["allocations"]["IDX_SPREADS"] == 250_000.0   # 25%
    assert res["allocations"]["EQ_SWING"] == 200_000.0      # 20% (cum = 100%)
    assert res["allocations"]["CROSS_ASSET"] == 0.0         # clipped so total <= 100%
    assert res["clipped_by_total_cap"] == ["CROSS_ASSET"]


def test_halted_bucket_capital_cannot_be_borrowed_by_any_other_bucket(monkeypatch):
    monkeypatch.setattr(buckets.config, "MULTI_BUCKET_LEDGER", True)
    # 1. Allocator-level proof: when DARLINGS (30% = Rs.3,00,000) is halted, its Rs.3,00,000
    # is quarantined; active buckets cannot borrow DARLINGS's Rs.3,00,000 to fund CROSS_ASSET
    # or exceed their own ceilings.
    res = buckets.allocate_bucket_ceilings(
        1_000_000.0,
        halted_families={"DARLINGS"},
    )
    assert res["quarantined_halted_rs"] == 300_000.0
    assert res["distributable_rs"] == 700_000.0
    assert res["allocations"]["DARLINGS"] == 0.0
    assert res["total_allocated_rs"] == 700_000.0
    assert res["allocations"]["CROSS_ASSET"] == 0.0

    # Even if only DARLINGS (halted) and CROSS_ASSET (active) are considered, CROSS_ASSET
    # is still strictly bounded by its own 15% ceiling (Rs.1,50,000) and cannot touch DARLINGS.
    res_low = buckets.allocate_bucket_ceilings(
        1_000_000.0,
        requested_by_family={
            "WEALTH_LOCK": 0.0,
            "DARLINGS": 300_000.0,
            "IDX_SPREADS": 0.0,
            "EQ_SWING": 0.0,
            "CROSS_ASSET": 450_000.0,  # tries to borrow halted DARLINGS's 300k + its own 150k
        },
        halted_families={"DARLINGS"},
    )
    assert res_low["allocations"]["DARLINGS"] == 0.0
    assert res_low["allocations"]["CROSS_ASSET"] == 150_000.0

    # 2. Runtime gate & firewall proof: halt DARLINGS on a 15% loss; verify can_borrow_from_bucket
    # refuses borrowing and IDX_SPREADS cannot spend DARLINGS's remaining cash.
    conn = brain_map.connect(":memory:")
    pm.ensure_accounts_schema(conn)
    pm.get_account(conn)
    for pid, fam, cap in (
        ("PAPER_10L/IDX_SPREADS", "index_options", 250_000.0),
        ("PAPER_10L/DARLINGS", "darlings", 300_000.0),
    ):
        conn.execute(
            "INSERT INTO portfolios (portfolio_id, account_id, strategy_family, starting_capital, "
            "realized_pnl, peak_equity, risk_per_trade_pct, max_drawdown_pct, daily_loss_pct, created_at) "
            "VALUES (?, 'PAPER_10L', ?, ?, 0, ?, 2.0, 10.0, 3.0, 'now')",
            (pid, fam, cap, cap),
        )
    conn.commit()

    pm.request_entry(conn, "eqd:halt1", 100_000.0, portfolio_id="PAPER_10L/DARLINGS")
    pm.release_margin(conn, "eqd:halt1", -35_000.0)  # >10% drawdown on DARLINGS -> halts DARLINGS
    assert buckets.bucket(conn, "PAPER_10L/DARLINGS")["halted_at"] is not None

    ok_borrow, why_borrow = buckets.can_borrow_from_bucket(
        conn, "PAPER_10L/IDX_SPREADS", "PAPER_10L/DARLINGS", 50_000.0
    )
    assert not ok_borrow
    assert "halted_bucket_quarantine" in why_borrow

    # IDX_SPREADS has Rs.2,50,000; asking for Rs.2,60,000 is rejected even though halted DARLINGS
    # still sits on Rs.2,65,000 of cash inside PAPER_10L.
    over_ask = pm.request_entry(conn, "opt_over", 260_000.0, portfolio_id="PAPER_10L/IDX_SPREADS")
    assert not over_ask["approved"]
    assert "margin exhaustion in bucket PAPER_10L/IDX_SPREADS" in over_ask["reason"]
    conn.close()


def test_migrate_m1_buckets_plan_parity_and_idempotent_apply():
    conn = brain_map.connect(":memory:")
    pm.ensure_accounts_schema(conn)
    pm.get_account(conn)
    pm.get_paper_account(conn, pm.ACCOUNT_PAPER_2L)
    conn.execute("CREATE TABLE IF NOT EXISTS treasury_state (id INTEGER PRIMARY KEY, equity_budget_rs REAL, updated_at TEXT)")
    conn.execute("INSERT INTO treasury_state (id, equity_budget_rs, updated_at) VALUES (1, 300000.0, 'now')")
    pm.request_entry(conn, "opt_m1", 40_000.0)
    pm.request_entry(conn, "eqd:m1", 25_000.0)
    pm.paper_request_entry(conn, pm.ACCOUNT_PAPER_2L, "opt_m1", 15_000.0, lots=1, primary_lots=2)

    p = migrate_m1_buckets.plan(conn)
    assert p["ok"] is True
    assert all(c["ok"] for c in p["parity"])

    counts1 = migrate_m1_buckets.apply(conn, p)
    assert counts1["buckets"] == 6
    assert counts1["locks"] == 3

    # Re-running apply is idempotent (0 new buckets or duplicate locks inserted).
    counts2 = migrate_m1_buckets.apply(conn, p)
    assert counts2["buckets"] == 0
    assert counts2["locks"] == 0
    conn.close()


def test_remote_kill_switch_discord_halt_owner_auth_and_latch_lifecycle(monkeypatch):
    from src import discord_bot as bot
    from src import equity_desk

    conn = brain_map.connect(":memory:")
    pm.ensure_accounts_schema(conn)
    pm.get_account(conn)
    pm.get_paper_account(conn, pm.ACCOUNT_PAPER_2L)
    conn.execute(
        "INSERT INTO portfolios (portfolio_id, account_id, strategy_family, starting_capital, "
        "realized_pnl, peak_equity, risk_per_trade_pct, max_drawdown_pct, daily_loss_pct, created_at) "
        "VALUES ('PAPER_10L/DARLINGS', 'PAPER_10L', 'darlings', 300000.0, 0, 300000.0, 2.0, 10.0, 3.0, 'now')"
    )
    conn.commit()

    # 1. Fail-closed when AUTHORIZED_DISCORD_USER_ID is unset or wrong caller ID
    monkeypatch.delenv("AUTHORIZED_DISCORD_USER_ID", raising=False)
    monkeypatch.setattr(bot, "_load_env", lambda: None)
    unauth1 = bot._execute_halt_command("trip", "all", "emergency", user_id=999999, conn=conn)
    assert unauth1["ok"] is False and unauth1["status"] == "unauthorized"
    assert not pm.halt_latched(conn) and not equity_desk.desk_halt_latched(conn)

    monkeypatch.setenv("AUTHORIZED_DISCORD_USER_ID", "123456789")
    unauth2 = bot._execute_halt_command("trip", "all", "intruder", user_id=999999, conn=conn)
    assert unauth2["ok"] is False and unauth2["status"] == "unauthorized"
    assert not pm.halt_latched(conn) and not equity_desk.desk_halt_latched(conn)

    # 2. Authorized owner trips the remote kill-switch across all scopes
    tripped = bot._execute_halt_command(
        "trip", "all", "mobile panic button test", user_id=123456789, conn=conn
    )
    assert tripped["ok"] is True and tripped["status"] == "tripped"
    assert pm.halt_latched(conn) is True
    assert equity_desk.desk_halt_latched(conn) is True
    assert pm.paper_halt_latched(conn, pm.ACCOUNT_PAPER_2L) is True
    assert pm.paper_halt_latched(conn, pm.ACCOUNT_PAPER_SHADOW_LEARNER) is False
    assert buckets.bucket(conn, "PAPER_10L/DARLINGS")["halted_at"] is not None
    reply_trip = bot._format_halt_reply(tripped)
    assert "REMOTE KILL-SWITCH LATCHED" in reply_trip

    # Verify entries are blocked while latched
    refused_primary = pm.request_entry(conn, "opt_after_halt", 25_000.0)
    assert refused_primary["approved"] is False
    refused_2l = pm.paper_request_entry(conn, pm.ACCOUNT_PAPER_2L, "opt_after_halt", 10_000.0)
    assert refused_2l["approved"] is False

    # 3. Status command reports all latched targets
    st = bot._execute_halt_command("status", "all", "", user_id=123456789, conn=conn)
    assert st["ok"] is True and st["targets"]["firm_PAPER_10L"]["latched"] is True
    assert st["targets"]["equity_desk_DARLINGS"]["latched"] is True
    assert "HALTED (latched)" in bot._format_halt_reply(st)

    # 4. Clearing without a `why` reason is refused (#92 / #125)
    no_why = bot._execute_halt_command("clear", "all", "   ", user_id=123456789, conn=conn)
    assert no_why["ok"] is False and no_why["status"] == "missing_why"
    assert pm.halt_latched(conn) is True

    # 5. Authorized clear with a valid `why` unlatches and audits the clear
    cleared = bot._execute_halt_command(
        "clear", "all", "false alarm verified on phone", user_id=123456789, conn=conn
    )
    assert cleared["ok"] is True and cleared["status"] == "cleared"
    assert pm.halt_latched(conn) is False
    assert equity_desk.desk_halt_latched(conn) is False
    assert pm.paper_halt_latched(conn, pm.ACCOUNT_PAPER_2L) is False
    assert buckets.bucket(conn, "PAPER_10L/DARLINGS")["halted_at"] is None
    assert "KILL-SWITCH CLEAR EXECUTED" in bot._format_halt_reply(cleared)
    conn.close()
