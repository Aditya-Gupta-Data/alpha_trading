"""
Tests for src/chat_agent — data-shaping boundaries.

Fully offline: every test uses an in-memory SQLite DB seeded via
brain_map.connect(':memory:') with the simulator + graph_engine schemas.
No Discord, no Ollama, no network, no real data/ files are touched.

Run:
    python tests/test_chat_agent.py
    pytest tests/test_chat_agent.py -v
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Provide a sentinel env var before chat_agent is imported so the module-level
# _AUTHORIZED_USER_ID parse does not raise in CI where .env may be absent.
import os
os.environ.setdefault("AUTHORIZED_DISCORD_USER_ID", "123456789")

from src import brain_map
from src.graph_engine import add_edge, ensure_schema as ensure_graph_schema
from src.simulator import ensure_schema as ensure_sim_schema

# Import the testable inner function — tests use this to inject their own
# already-seeded in-memory connection instead of opening a DB file.
from src.chat_agent import _context_from_conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_conn() -> sqlite3.Connection:
    """Fresh in-memory DB with both the simulator and graph_engine schemas."""
    conn = brain_map.connect(":memory:")
    ensure_sim_schema(conn)
    ensure_graph_schema(conn)
    return conn


def _insert_trade(conn, *, underlying="NIFTY 50", strategy="iron_condor",
                  result="win", pnl=1500.0, r_multiple=1.5,
                  proposed_on="2025-06-01") -> None:
    ref = f"sim:{underlying}|{proposed_on}"
    conn.execute(
        "INSERT OR IGNORE INTO simulated_trades "
        "(journal_ref, underlying, strategy, view, proposed_on, expiry, "
        "resolution, exit_date, pnl_net, frictions_rs, slippage_rs, "
        "r_multiple, result) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ref, underlying, strategy, "NEUTRAL", proposed_on, "2025-06-26",
         "PROFIT_TARGET", "2025-06-15", pnl, 50.0, 20.0, r_multiple, result),
    )
    conn.commit()


def _insert_edge(conn, src="NIFTY 50", rel="led_to", tgt="IT_STRENGTH",
                 conf=0.85) -> None:
    add_edge(conn, src, rel, tgt, confidence_score=conf)


# ---------------------------------------------------------------------------
# Tests — simulated_trades shaping
# ---------------------------------------------------------------------------

def test_empty_db_returns_no_data():
    conn = _make_conn()
    assert _context_from_conn(conn) == "(no data)"


def test_single_trade_formats_correctly():
    conn = _make_conn()
    _insert_trade(conn, underlying="NIFTY 50", strategy="iron_condor",
                  result="win", pnl=2000.0, r_multiple=1.8,
                  proposed_on="2025-06-10")
    ctx = _context_from_conn(conn)
    assert ctx.startswith("sim|NIFTY 50|iron_condor|win")
    assert "pnl=2000" in ctx
    assert "r=1.80" in ctx
    assert "on=2025-06-10" in ctx


def test_returns_at_most_three_trades():
    conn = _make_conn()
    for i in range(5):
        _insert_trade(conn, proposed_on=f"2025-06-{10 + i:02d}")
    ctx = _context_from_conn(conn)
    sim_lines = [l for l in ctx.splitlines() if l.startswith("sim|")]
    assert len(sim_lines) == 3


def test_trades_ordered_most_recent_first():
    conn = _make_conn()
    _insert_trade(conn, proposed_on="2025-06-01", pnl=100.0)
    _insert_trade(conn, proposed_on="2025-06-15", pnl=900.0)
    _insert_trade(conn, proposed_on="2025-06-10", pnl=500.0)
    ctx = _context_from_conn(conn)
    sim_lines = [l for l in ctx.splitlines() if l.startswith("sim|")]
    # Most recent first: 06-15, 06-10, 06-01
    assert "on=2025-06-15" in sim_lines[0]
    assert "on=2025-06-10" in sim_lines[1]
    assert "on=2025-06-01" in sim_lines[2]


def test_null_r_multiple_renders_as_na():
    conn = _make_conn()
    _insert_trade(conn, r_multiple=None)
    ctx = _context_from_conn(conn)
    assert "r=n/a" in ctx


def test_loss_trade_result_present():
    conn = _make_conn()
    _insert_trade(conn, result="loss", pnl=-800.0)
    ctx = _context_from_conn(conn)
    assert "|loss|" in ctx
    assert "pnl=-800" in ctx


# ---------------------------------------------------------------------------
# Tests — graph_edges shaping
# ---------------------------------------------------------------------------

def test_single_edge_formats_correctly():
    conn = _make_conn()
    _insert_edge(conn, src="NIFTY 50", rel="led_to", tgt="IT_STRENGTH", conf=0.9)
    ctx = _context_from_conn(conn)
    assert "edge|NIFTY 50|led_to|IT_STRENGTH|conf=0.90" in ctx


def test_edge_with_null_confidence_renders_as_na():
    conn = _make_conn()
    add_edge(conn, "VIX", "signals", "FEAR", confidence_score=None)
    ctx = _context_from_conn(conn)
    assert "edge|VIX|signals|FEAR|conf=n/a" in ctx


def test_returns_at_most_three_edges():
    conn = _make_conn()
    for i in range(6):
        add_edge(conn, f"NODE_{i}", "linked_to", f"TARGET_{i}", confidence_score=0.5)
    ctx = _context_from_conn(conn)
    edge_lines = [l for l in ctx.splitlines() if l.startswith("edge|")]
    assert len(edge_lines) == 3


# ---------------------------------------------------------------------------
# Tests — graceful degradation
# ---------------------------------------------------------------------------

def test_missing_simulated_trades_table_does_not_raise():
    # Only the graph schema — no simulator schema
    conn = brain_map.connect(":memory:")
    ensure_graph_schema(conn)
    _insert_edge(conn)
    ctx = _context_from_conn(conn)
    # Should still return the edge line without crashing
    assert "edge|" in ctx


def test_missing_graph_edges_table_does_not_raise():
    # Only the simulator schema — no graph schema
    conn = brain_map.connect(":memory:")
    ensure_sim_schema(conn)
    _insert_trade(conn)
    ctx = _context_from_conn(conn)
    # Should still return the sim line without crashing
    assert "sim|" in ctx


def test_graph_edges_without_invalid_at_column_falls_back():
    """Verify the two-SQL fallback: a graph_edges table that lacks the
    invalid_at column (future schema addition) must still return rows."""
    conn = brain_map.connect(":memory:")
    # Build graph_edges WITHOUT the invalid_at column
    conn.execute(
        "CREATE TABLE graph_edges ("
        "  source_node TEXT, relation TEXT, target_node TEXT, "
        "  confidence_score REAL"
        ")"
    )
    conn.execute(
        "INSERT INTO graph_edges VALUES ('A', 'causes', 'B', 0.7)"
    )
    conn.commit()
    ctx = _context_from_conn(conn)
    assert "edge|A|causes|B|conf=0.70" in ctx


def test_trades_and_edges_both_present():
    conn = _make_conn()
    _insert_trade(conn, pnl=1200.0)
    _insert_edge(conn, conf=0.75)
    ctx = _context_from_conn(conn)
    lines = ctx.splitlines()
    assert any(l.startswith("sim|") for l in lines)
    assert any(l.startswith("edge|") for l in lines)


# ---------------------------------------------------------------------------
# Phase 6J: the "@ADiTrader portfolio" snapshot command (no LLM involved)
# ---------------------------------------------------------------------------

from src.chat_agent import build_portfolio_snapshot, is_portfolio_command


def test_portfolio_command_detection_is_exact():
    assert is_portfolio_command("portfolio")
    assert is_portfolio_command("  Portfolio  ")       # whitespace/case only
    assert not is_portfolio_command("portfolio please")
    assert not is_portfolio_command("show my portfolio")
    assert not is_portfolio_command("")


def test_snapshot_formats_the_injected_summary_verbatim():
    text = build_portfolio_snapshot(summary={
        "starting_capital": 1_000_000.0, "available_cash": 715_500.5,
        "locked_margin": 284_499.5, "open_locks": 3, "realized_pnl": 0.0,
    })
    assert "Starting Capital: Rs.1,000,000.00" in text
    assert "Free Cash: Rs.715,500.50" in text
    assert "Locked Margin: Rs.284,499.50" in text
    assert "Active Trades: 3" in text
    assert "Net PnL: Rs.0.00" in text


def test_snapshot_reads_the_capital_layer_end_to_end():
    from src import portfolio_manager as pm
    conn = brain_map.connect(":memory:")
    pm.request_entry(conn, "t1", 250_000.0)
    pm.request_entry(conn, "t2", 100_000.0)
    pm.request_entry(conn, "closed", 50_000.0)
    pm.release_margin(conn, "closed", pnl_net=12_000.0)
    text = build_portfolio_snapshot(conn=conn)
    assert "Starting Capital: Rs.1,000,000.00" in text
    assert "Locked Margin: Rs.350,000.00" in text       # two active locks
    assert "Free Cash: Rs.662,000.00" in text           # 10L + 12k - 3.5L
    assert "Active Trades: 2" in text                   # closed lock released
    assert "Net PnL: Rs.12,000.00" in text


def test_snapshot_command_never_calls_ollama():
    """The routing contract: a portfolio query resolves without the LLM —
    the snapshot builder works with Ollama booby-trapped."""
    from src import chat_agent as ca
    saved = ca._call_ollama

    async def forbidden(*a, **k):
        raise AssertionError("portfolio command reached Ollama!")

    try:
        ca._call_ollama = forbidden
        assert is_portfolio_command("portfolio")
        text = build_portfolio_snapshot(summary={
            "starting_capital": 1_000_000.0, "available_cash": 1_000_000.0,
            "locked_margin": 0.0, "open_locks": 0, "realized_pnl": 0.0})
        assert text.startswith("**ADiTrader Portfolio Snapshot**")
    finally:
        ca._call_ollama = saved


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as exc:
            print(f"  FAIL  {fn.__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    if failed:
        raise SystemExit(1)
