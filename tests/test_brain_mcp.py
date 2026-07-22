"""Brain-Map MCP server, fully offline: a miniature brain + artifact set
in tmp_path, messages driven straight through handle_message (no
subprocess, no network). The posture tests are the point: read-only by
construction, no advice verbs on the tool surface, honest misses."""
import io
import json
import sqlite3

import pytest

from src import brain_mcp
from src.brain_mcp import Sources, handle_message, serve


@pytest.fixture()
def src(tmp_path):
    """A tiny but schema-true brain + artifacts under tmp_path."""
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "brain_map.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE events (id INTEGER PRIMARY KEY, date TEXT, ticker TEXT,
            event_type TEXT, tag TEXT, sentiment TEXT, source TEXT);
        CREATE TABLE outcomes (id INTEGER PRIMARY KEY, date TEXT,
            ticker TEXT, archetype TEXT, r_multiple REAL, result TEXT);
        CREATE TABLE event_outcome_link (event_id INT, outcome_id INT);
        CREATE TABLE entity_affinity (client TEXT, grp TEXT,
            deal_count INT, buy_qty INT, sell_qty INT, buy_value_rs REAL,
            sell_value_rs REAL, first_seen TEXT, last_seen TEXT);
        CREATE TABLE daily_context (date TEXT, vix REAL, vix_band TEXT,
            macro_nifty_short REAL, macro_nifty_medium REAL,
            macro_bank_short REAL, macro_bank_medium REAL, news_net INT,
            fii_net REAL, dii_net REAL);
        CREATE TABLE equity_curve (ts TEXT, equity REAL, peak_equity REAL,
            drawdown_pct REAL);
        CREATE TABLE simulated_trades (strategy TEXT, pnl_net REAL,
            r_multiple REAL, result TEXT);
        INSERT INTO events VALUES
            (1,'2026-07-01','TCS','news','it_deal','positive','rss');
        INSERT INTO outcomes VALUES
            (7,'2026-07-08','TCS','breakout',1.4,'win');
        INSERT INTO event_outcome_link VALUES (1,7);
        INSERT INTO entity_affinity VALUES
            ('BIG FUND','TCS',9,100,0,5.0e7,0,'2026-06-01','2026-07-01');
        INSERT INTO daily_context VALUES
            ('2026-07-22',14.2,'mid',0.4,0.6,0.1,0.2,3,120.5,-40.0);
        INSERT INTO equity_curve VALUES ('2026-07-22',200000,200000,0.0);
        INSERT INTO simulated_trades VALUES
            ('iron_condor',1500,0.5,'win'), ('iron_condor',-900,-0.3,'loss');
    """)
    conn.commit()
    conn.close()
    (tmp_path / "data" / "darling_tiers.json").write_text(json.dumps(
        {"as_of": "2026-07-22", "counts": {"weak_buy": 1},
         "tiers": {"weak_buy": [{"ticker": "TCS"}]}}))
    (tmp_path / "data" / "darlings_valuation.json").write_text(json.dumps(
        {"as_of": "2026-07-22", "universe_n": 1, "scores": {"TCS": 35},
         "vetoed": {"XYZ": "negative TTM EPS"}}))
    (tmp_path / "data" / "fo_liquidity.json").write_text(json.dumps(
        {"as_of": "2026-07-22", "banned": ["KAYNES"], "tier_rule": "top25",
         "symbols": {"TCS": {"tier": 1}}}))
    rep = tmp_path / "data" / "lake" / "fundamental_reports" / "TCS"
    rep.mkdir(parents=True)
    (rep / "FY25.json").write_text(json.dumps(
        {"ticker": "TCS", "fiscal_year": "FY25", "conviction_score": 8,
         "sub_scores": {"qoe": 9}, "red_flags": [], "yellow_flags": ["x"],
         "hidden_debt_flags": []}))
    (rep / "FY24.json").write_text(json.dumps(
        {"ticker": "TCS", "fiscal_year": "FY24", "conviction_score": 6}))
    return Sources(root=tmp_path)


def _call(src, tool, arguments=None, msg_id=1):
    resp = handle_message({"jsonrpc": "2.0", "id": msg_id,
                           "method": "tools/call",
                           "params": {"name": tool,
                                      "arguments": arguments or {}}}, src)
    assert resp["result"]["isError"] is False, resp
    return json.loads(resp["result"]["content"][0]["text"])


def test_initialize_and_tools_list_speak_mcp(src):
    init = handle_message({"jsonrpc": "2.0", "id": 0,
                           "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18"}}, src)
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert "tools" in init["result"]["capabilities"]
    listed = handle_message({"jsonrpc": "2.0", "id": 1,
                             "method": "tools/list"}, src)
    names = {t["name"] for t in listed["result"]["tools"]}
    assert names == set(brain_mcp.TOOLS)
    assert all(t["inputSchema"]["type"] == "object"
               for t in listed["result"]["tools"])


def test_tool_surface_carries_no_advice_verbs():
    """The SEBI posture, as a test: the tool surface (names +
    descriptions) states facts, never tells anyone what to do."""
    banned = ("buy", "sell", "recommend", "should", "advice", "invest")
    for name, (desc, _, _) in brain_mcp.TOOLS.items():
        surface = f"{name} {desc}".lower()
        for verb in banned:
            assert verb not in surface, (name, verb)


def test_event_history_joins_measured_outcomes(src):
    out = _call(src, "event_history", {"ticker": "tcs"})
    assert out["available"] and len(out["events"]) == 1
    ev = out["events"][0]
    assert ev["tag"] == "it_deal"
    assert ev["measured_outcomes"][0]["r_multiple"] == 1.4


def test_earnings_quality_serves_newest_year_and_honest_miss(src):
    out = _call(src, "earnings_quality", {"ticker": "TCS"})
    assert out["fiscal_year"] == "FY25" and out["conviction_score"] == 8
    miss = _call(src, "earnings_quality", {"ticker": "NOSUCH"})
    assert miss["available"] is False


def test_valuation_and_liquidity_and_tiers_read_artifacts(src):
    val = _call(src, "valuation_scores", {"ticker": "tcs"})
    assert val["score"] == 35 and val["vetoed"] is False
    veto = _call(src, "valuation_scores", {"ticker": "XYZ"})
    assert veto["vetoed"] is True and "EPS" in veto["veto_reason"]
    liq = _call(src, "fo_liquidity", {"ticker": "KAYNES"})
    assert liq["banned"] is True
    tiers = _call(src, "darling_tier_table")
    assert tiers["counts"] == {"weak_buy": 1}


def test_regime_curve_and_stats_stamp_their_basis(src):
    reg = _call(src, "market_regime")
    assert reg["latest_context"]["vix_band"] == "mid"
    curve = _call(src, "firm_equity_curve")
    assert curve["points"][0]["equity"] == 200000
    assert "paper" in curve["basis"]
    stats = _call(src, "strategy_stats")
    row = stats["by_strategy"][0]
    assert row["n"] == 2 and row["wins"] == 1
    assert "never expected return" in stats["basis"]


def test_missing_artifacts_answer_honestly_not_fatally(tmp_path):
    (tmp_path / "data").mkdir()
    bare = Sources(root=tmp_path)          # no db, no artifacts
    out = _call(bare, "darling_tier_table")
    assert out["available"] is False
    resp = handle_message({"jsonrpc": "2.0", "id": 2,
                           "method": "tools/call",
                           "params": {"name": "event_history",
                                      "arguments": {"ticker": "TCS"}}}, bare)
    assert resp["result"]["isError"] is True   # db missing → error, not death


def test_server_is_readonly_by_construction(src):
    with pytest.raises(sqlite3.OperationalError):
        src.conn().execute("INSERT INTO events VALUES "
                           "(9,'x','X','t','t','s','s')")


def test_unknown_tool_and_method_and_notification(src):
    bad = handle_message({"jsonrpc": "2.0", "id": 3,
                          "method": "tools/call",
                          "params": {"name": "nope"}}, src)
    assert bad["error"]["code"] == -32602
    missing = handle_message({"jsonrpc": "2.0", "id": 4,
                              "method": "resources/list"}, src)
    assert missing["error"]["code"] == -32601
    assert handle_message({"jsonrpc": "2.0",
                           "method": "notifications/initialized"}, src) is None


def test_serve_round_trips_over_text_streams_and_skips_junk(src):
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                    "params": {}}),
        "this is not json",
        json.dumps({"jsonrpc": "2.0", "method":
                    "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
    ]
    out = io.StringIO()
    serve(inp=io.StringIO("\n".join(lines) + "\n"), out=out, src=src)
    replies = [json.loads(l) for l in out.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [0, 1]     # junk + notification silent
    assert replies[1]["result"]["tools"]
