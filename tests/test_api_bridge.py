"""Decision #113 — the read-only JSON bridge behind the React desk."""
import json
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.dashboard import api_bridge, data as d


@pytest.fixture
def client(tmp_path, monkeypatch):
    from src import brain_map, portfolio_manager as pm
    db = tmp_path / "bm.db"
    c = brain_map.connect(str(db)); pm.get_account(c); pm.get_paper_account(c, "PAPER_2L")
    pm.request_entry(c, "ab12cd34", 50000.0); c.close()
    (tmp_path / "journal.jsonl").write_text(json.dumps(
        {"short_id": "ab12cd34", "ticker": "NIFTY 50", "date": "2026-09-16", "decision": "approved", "outcome": None,
         "spread": {"strategy": "bear_put_spread", "direction": "bearish", "lots": 2, "max_loss": 5000.0, "expiry": "2026-10-27"},
         "ratchet": {"peak_capture_pct": 51.45, "locked_pct": 0.0, "armed": True}}) + "\n")
    (tmp_path / "recon.jsonl").write_text(json.dumps(
        {"ts": "2026-09-23T11:43:41", "verdict": "parity", "broker_positions": 0, "book_rows": 15,
         "mismatches": [{"kind": "broker_position_unknown_to_book", "detail": "🔴 RECON MISMATCH: x"}]}) + "\n")
    monkeypatch.setattr(d, "DB_PATH", db)
    monkeypatch.setattr(d, "JOURNAL_PATH", tmp_path / "journal.jsonl")
    monkeypatch.setattr(d, "EQUITY_LEDGER_PATH", tmp_path / "eq.jsonl")
    monkeypatch.setattr(d, "SNAPSHOT_PATH", tmp_path / "snap.json")
    monkeypatch.setattr(d, "RECON_PATH", tmp_path / "recon.jsonl")
    monkeypatch.setenv("DASHBOARD_KEY", "s3cret")
    return TestClient(api_bridge.app)


def test_every_route_needs_the_key_and_health_does_not(client):
    for path in ("/api/treasury", "/api/open-trades", "/api/recent-outcomes", "/api/recon/latest",
                 "/api/recon/history", "/api/audit", "/api/freshness"):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers={"X-Access-Key": "wrong"}).status_code == 401, path
        assert client.get(path, headers={"X-Access-Key": "s3cret"}).status_code == 200, path
    assert client.get("/api/health").json() == {"ok": True, "paper_only": True, "writes": False}


def test_shapes_match_the_ui_contract(client):
    h = {"X-Access-Key": "s3cret"}
    t = client.get("/api/treasury", headers=h).json()
    assert t["PAPER_10L"]["equity"] == 1_000_000.0 and t["PAPER_10L"]["rejections"] == 0
    assert "drawdown_pct" in t["PAPER_10L"] and "cagr_pct" in t["PAPER_10L"]   # CAGR re-introduced (decision #114)
    ot = client.get("/api/open-trades", headers=h).json()
    assert ot[0]["strategy"] == "Bear Put" and ot[0]["ratchet_lock_pct"] == 0.0
    r = client.get("/api/recon/latest", headers=h).json()
    assert r["verdict"] == "PARITY" and r["mismatches"] == ["🔴 RECON MISMATCH: x"]
    assert client.get("/api/recon/history", headers=h).json()[0]["verdict"] == "PARITY"
    assert isinstance(client.get("/api/audit", headers=h).json(), list)
    assert set(client.get("/api/freshness", headers=h).json()) == {"brain_map.db", "journal", "market_snapshot", "recon"}


def test_no_key_configured_means_open_local_dev(client, monkeypatch):
    monkeypatch.delenv("DASHBOARD_KEY")
    assert client.get("/api/freshness").status_code == 200


def test_bridge_is_get_only_and_imports_no_execution_path():
    src = Path(api_bridge.__file__).read_text()
    assert "@app.post" not in src and "@app.put" not in src and "@app.delete" not in src
    for pat in ("paper_venue", "strategy_router", "oms", "dhan_client", "fire_broadcast", "journal.rewrite"):
        assert not re.search(r"\b" + pat + r"\b", src), pat
    routes = sorted(r.path for r in api_bridge.app.routes if r.path.startswith("/api"))
    assert routes == ["/api/audit", "/api/freshness", "/api/health", "/api/open-trades",
                      "/api/recent-outcomes", "/api/recon/history", "/api/recon/latest", "/api/treasury"]


def test_naive_ist_timestamps_are_stamped_and_dates_untouched(client):
    assert api_bridge._ist("2026-09-24T09:21:59") == "2026-09-24T09:21:59+05:30"
    assert api_bridge._ist("2026-09-23T21:57") == "2026-09-23T21:57+05:30"
    assert api_bridge._ist("2026-09-24") == "2026-09-24"                       # a session date
    assert api_bridge._ist("2026-09-24T15:29:11+05:30") == "2026-09-24T15:29:11+05:30"
    assert api_bridge._ist(None) is None
    h = {"X-Access-Key": "s3cret"}
    r = client.get("/api/recon/latest", headers=h).json()
    assert r["ts"] == "2026-09-23T11:43:41+05:30"
    fr = client.get("/api/freshness", headers=h).json()
    assert fr["journal"].endswith("+05:30")


def test_every_response_is_no_store_and_read_fresh_per_request(client, tmp_path, monkeypatch):
    h = {"X-Access-Key": "s3cret"}
    r1 = client.get("/api/recon/latest", headers=h)
    assert r1.headers["cache-control"] == "no-store"
    # the mirror file changes between two requests -> the second sees it
    Path(d.RECON_PATH).write_text(Path(d.RECON_PATH).read_text() + json.dumps(
        {"ts": "2026-09-24T17:20:00", "verdict": "mismatch", "broker_positions": 1, "book_rows": 15,
         "mismatches": [{"detail": "x"}]}) + "\n")
    r2 = client.get("/api/recon/latest", headers=h).json()
    assert r2["verdict"] == "MISMATCH" and r2["ts"].endswith("+05:30")
