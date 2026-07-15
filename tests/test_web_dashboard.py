"""
Tests for the Phase 6 event-driven web dashboard: the /api/web/* routes,
the SSE change-watcher, and the single-file frontend's anti-polling
contract. Offline — journal/marks/exposure mocked, SSE generator driven
with temp files and millisecond poll intervals.

Run:
    python tests/test_web_dashboard.py
    pytest tests/test_web_dashboard.py -v
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from src import api as api_module
from src.api import app

STATIC_DASHBOARD = (Path(__file__).resolve().parent.parent
                    / "src" / "web" / "static" / "dashboard.html")


def _client() -> TestClient:
    return TestClient(app)


def _no_key_env():
    env = {k: v for k, v in os.environ.items() if k != "API_KEY"}
    return mock.patch.dict(os.environ, env, clear=True)


def _spread_entry(short_id="sprd0001"):
    return {
        "short_id": short_id, "date": "2026-07-08", "action": "SPREAD",
        "ticker": "NIFTY 50", "shares": 75, "price": 72.3,
        "decision": "approved",
        "spread": {"strategy": "bear_put_spread",
                   "legs": [{"side": "BUY", "option_type": "PE",
                             "strike": 24150.0, "premium": 181.75}],
                   "lot_size": 75, "lots": 1, "expiry": "2026-07-21",
                   "net_debit": 72.3, "net_credit": None,
                   "max_loss": 5422.5, "max_profit": 9577.5},
        "outcome": None,
    }


# ------------------------------------------------------- /api/web/positions

def test_web_positions_returns_marked_positions_and_exposure():
    marked = [{"short_id": "sprd0001", "ticker": "NIFTY 50",
               "strategy": "bear_put_spread", "live_pnl_rs": 4200.0,
               "detail": "44% of max profit, 11d to expiry"}]
    exposure = {"locked_margin_rs": 20422.5, "equity_rs": 1_000_000.0,
                "exposure_pct": 2.04}
    with _no_key_env(), \
         mock.patch("src.positions.journal.read_all",
                    return_value=[_spread_entry()]), \
         mock.patch("src.portfolio_report._open_entries",
                    return_value=([_spread_entry()], [])), \
         mock.patch("src.portfolio_report.mark_positions",
                    return_value=marked), \
         mock.patch("src.portfolio_report.read_exposure",
                    return_value=exposure), \
         mock.patch("src.dhan_guard.SafeDhanClient"):
        body = _client().get("/api/web/positions").json()
    assert body["ok"] is True
    assert len(body["positions"]) == 1
    p = body["positions"][0]
    assert p["trade_id"] == "sprd0001"
    assert p["live_pnl_rs"] == 4200.0
    assert "44%" in p["live_detail"]
    assert body["exposure"] == exposure
    assert body["as_of"]


def test_web_positions_degrades_to_nulls_when_marks_and_exposure_fail():
    """No token / closed market / missing tables must yield nulls, never
    an error — the dashboard's read must always succeed."""
    with _no_key_env(), \
         mock.patch("src.positions.journal.read_all",
                    return_value=[_spread_entry()]), \
         mock.patch("src.portfolio_report._open_entries",
                    side_effect=RuntimeError("no quotes for you")), \
         mock.patch("src.portfolio_report.read_exposure",
                    side_effect=RuntimeError("db gone")):
        resp = _client().get("/api/web/positions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["positions"][0]["live_pnl_rs"] is None
    assert body["positions"][0]["live_detail"] is None
    assert body["exposure"] is None


def test_web_positions_respects_the_api_key_gate_when_configured():
    with mock.patch.dict(os.environ, {"API_KEY": "sekret"}, clear=False):
        denied = _client().get("/api/web/positions")
        allowed = _client().get("/api/web/positions",
                                headers={"X-API-Key": "sekret"})
    assert denied.status_code == 401
    assert allowed.status_code == 200


def test_api_key_query_param_authenticates_like_the_header():
    """EventSource cannot send headers, so behind the gateway the key
    rides as ?api_key= on the SSE and fetch URLs — it must authenticate
    exactly like X-API-Key, and a wrong value must still 401."""
    with mock.patch.dict(os.environ, {"API_KEY": "sekret"}, clear=False):
        assert _client().get("/api/web/positions?api_key=wrong").status_code == 401
        allowed = _client().get("/api/web/positions?api_key=sekret")
        page = _client().get("/dashboard?api_key=sekret")
    assert allowed.status_code == 200
    assert page.status_code == 200


def test_dashboard_page_propagates_the_key_to_both_endpoints():
    """The page reads ?api_key= off its own URL and appends it to the
    positions fetch AND the EventSource URL — keyless dev unchanged."""
    html = STATIC_DASHBOARD.read_text()
    assert "window.location.search" in html
    assert '"/api/web/positions" + KEY_QS' in html
    assert '"/api/web/events" + KEY_QS' in html


# ------------------------------------------------------------- /api/web/pnl

def test_web_pnl_composes_realized_open_and_net():
    marked = [{"short_id": "sprd0001", "live_pnl_rs": 4200.0},
              {"short_id": "sprd0002", "live_pnl_rs": -1200.0},
              {"short_id": "sprd0003", "live_pnl_rs": None}]  # unpriced
    exposure = {"locked_margin_rs": 20422.5, "equity_rs": 1_000_000.0,
                "realized_pnl_rs": 15000.0, "exposure_pct": 2.04}
    with _no_key_env(), \
         mock.patch("src.api.journal.read_all", return_value=[]), \
         mock.patch("src.portfolio_report.get_live_marks",
                    return_value=(marked, "snapshot")), \
         mock.patch("src.portfolio_report.read_exposure",
                    return_value=exposure):
        body = _client().get("/api/web/pnl").json()
    assert body["ok"] is True
    assert body["realized_pnl_rs"] == 15000.0
    assert body["open_pnl_rs"] == 3000.0            # 4200 - 1200, unpriced excluded
    assert body["net_pnl_rs"] == 18000.0            # realized + open
    assert body["open_marked"] == 2
    assert body["open_unmarked"] == 1
    assert body["exposure"] == exposure


def test_web_pnl_open_is_null_not_zero_when_nothing_priced():
    """A book with positions but no live quotes must read open=null, not
    a fake 0.0 — the strip must not imply a flat P&L it doesn't know."""
    marked = [{"short_id": "sprd0001", "live_pnl_rs": None}]
    with _no_key_env(), \
         mock.patch("src.api.journal.read_all", return_value=[]), \
         mock.patch("src.portfolio_report.get_live_marks",
                    return_value=(marked, None)), \
         mock.patch("src.portfolio_report.read_exposure",
                    return_value={"realized_pnl_rs": 500.0,
                                  "locked_margin_rs": 0.0, "equity_rs": 1.0,
                                  "exposure_pct": 0.0}):
        body = _client().get("/api/web/pnl").json()
    assert body["open_pnl_rs"] is None
    assert body["open_unmarked"] == 1
    assert body["net_pnl_rs"] == 500.0              # realized only, open unknown


def test_web_pnl_degrades_to_nulls_when_marks_and_exposure_fail():
    with _no_key_env(), \
         mock.patch("src.api.journal.read_all", return_value=[]), \
         mock.patch("src.portfolio_report.get_live_marks",
                    side_effect=RuntimeError("no token")), \
         mock.patch("src.portfolio_report.read_exposure",
                    side_effect=RuntimeError("db gone")):
        resp = _client().get("/api/web/pnl")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["realized_pnl_rs"] is None
    assert body["open_pnl_rs"] is None
    assert body["net_pnl_rs"] is None


# ------------------------------------------------------- /api/web/departments

def test_web_departments_maps_signals_without_faking_greens():
    """Real signals only: a silent scheduled log -> down, a problem line
    today -> warn, a clean monitored dept -> up, an inline dept -> inline
    (never a fabricated green), and Interfaces is up by answering."""
    with _no_key_env(), \
         mock.patch("src.ops_monitor.check_heartbeats",
                    return_value=["sleep_phase.log — did not run today"]), \
         mock.patch("src.ops_monitor.system_telemetry",
                    return_value={"mem_used_pct": 41, "disk_free_gb": 88.0,
                                  "load_1m": 0.7}):
        body = _client().get("/api/web/departments").json()
    by_name = {d["name"]: d for d in body["departments"]}
    assert body["ok"] is True
    assert len(body["departments"]) == 7
    assert by_name["Memory & Learning"]["status"] == "down"   # its log is silent
    assert by_name["Data"]["status"] == "up"                  # no silent log, no problems
    assert by_name["Risk & Capital"]["status"] == "inline"    # no scheduled log
    assert by_name["Validation"]["status"] == "inline"
    assert by_name["Reporting"]["status"] == "inline"
    assert by_name["Interfaces"]["status"] == "up"            # answered the request
    assert body["host"]["mem_used_pct"] == 41


def test_web_departments_reports_unknown_when_heartbeats_unavailable():
    """If the heartbeat check itself blows up, monitored departments must
    read 'unknown' — never a silent green that hides a broken monitor."""
    with _no_key_env(), \
         mock.patch("src.ops_monitor.check_heartbeats",
                    side_effect=RuntimeError("logs dir gone")), \
         mock.patch("src.ops_monitor.system_telemetry", return_value={}):
        body = _client().get("/api/web/departments").json()
    by_name = {d["name"]: d for d in body["departments"]}
    assert by_name["Data"]["status"] == "unknown"
    assert by_name["Decision"]["status"] == "unknown"
    assert by_name["Interfaces"]["status"] == "up"     # still self-evident
    assert by_name["Risk & Capital"]["status"] == "inline"


# ------------------------------------------------------------- /dashboard

def test_dashboard_route_serves_the_single_file_page():
    with _no_key_env():
        resp = _client().get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "ADiTrader" in resp.text
    assert "Manual Status Check" in resp.text


def test_dashboard_frontend_honors_the_anti_polling_rule():
    """The contract in code: no timer-based polling anywhere in the page —
    refreshes come only from load, the button, and SSE messages."""
    html = STATIC_DASHBOARD.read_text()
    assert "setInterval" not in html
    assert "setTimeout" not in html          # no sneaky timer-loop either
    assert "EventSource" in html             # Mechanism B present
    assert "/api/web/positions" in html      # Mechanism A fetch target
    assert "/api/web/events" in html


def test_dashboard_frontend_has_the_single_pane_strips():
    """The P&L and department strips live on the SAME page (single pane of
    glass) and fetch their own read-only endpoints, propagating the key."""
    html = STATIC_DASHBOARD.read_text()
    assert 'id="pnl-strip"' in html
    assert 'id="dept-strip"' in html
    assert '"/api/web/pnl" + KEY_QS' in html
    assert '"/api/web/departments" + KEY_QS' in html


# ------------------------------------------------------------- SSE watcher

def test_state_snapshot_reads_mtimes_and_tolerates_missing_files():
    with tempfile.TemporaryDirectory() as tmp:
        present = Path(tmp) / "journal.jsonl"
        present.write_text("{}\n")
        missing = Path(tmp) / "not_there.log"
        snap = api_module._dashboard_state_snapshot([present, missing])
    assert snap[str(present)] > 0
    assert snap[str(missing)] == 0


def test_event_stream_emits_connected_then_changed_on_file_touch():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            watched = Path(tmp) / "journal.jsonl"
            watched.write_text("line1\n")
            gen = api_module._dashboard_event_stream(
                poll_seconds=0.01, max_events=2, paths=[watched])
            first = await gen.__anext__()
            # mutate the watched file — the next poll must notice
            watched.write_text("line1\nline2\n")
            os.utime(watched, ns=(watched.stat().st_atime_ns,
                                  watched.stat().st_mtime_ns + 1))
            second = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
            return first, second

    first, second = asyncio.run(scenario())
    assert first.startswith("data: ")
    assert '"connected"' in first
    assert '"changed"' in second


def test_event_stream_stays_silent_while_nothing_changes():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            watched = Path(tmp) / "journal.jsonl"
            watched.write_text("static\n")
            gen = api_module._dashboard_event_stream(
                poll_seconds=0.01, max_events=2, paths=[watched])
            await gen.__anext__()                    # connected
            try:
                await asyncio.wait_for(gen.__anext__(), timeout=0.15)
                return "emitted"
            except asyncio.TimeoutError:
                return "silent"

    assert asyncio.run(scenario()) == "silent"


def test_event_stream_endpoint_declares_sse_content_type():
    """Route wiring test: the real generator is infinite (TestClient
    would deadlock closing it), so swap in a finite one and read the
    whole finite stream normally."""
    async def finite_stream():
        yield 'data: {"event": "connected"}\n\n'

    with _no_key_env(), \
         mock.patch.object(api_module, "_dashboard_event_stream",
                           return_value=finite_stream()):
        resp = _client().get("/api/web/events")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert '"connected"' in resp.text


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError:
            print(f"FAIL  {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed.")
