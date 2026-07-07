"""
Tests for the Phase 9 public gateway (src/api_server.py): strict fail-closed
API-key auth + the two-way Discord bridge endpoint.

Offline — FastAPI TestClient only; the journal is mocked (never touches
data/journal.jsonl, per HANDOVER's "never reset live data" rule) and the
Discord webhook is mocked.

Run:
    python tests/test_api_server.py
    pytest tests/test_api_server.py -v
"""

import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from src.api_server import app
from src import options_proposer as op

KEY = "gateway-secret"
AUTH = {"X-API-Key": KEY}


def _client() -> TestClient:
    return TestClient(app)


def _env_with_key():
    return mock.patch.dict(os.environ, {"API_KEY": KEY}, clear=False)


def _env_without_key():
    env = {k: v for k, v in os.environ.items() if k != "API_KEY"}
    return mock.patch.dict(os.environ, env, clear=True)


def make_pending_entry(short_id="pend0001", decision="pending_approval",
                       outcome=None):
    return {
        "short_id": short_id,
        "date": "2026-07-06",
        "action": "SPREAD",
        "ticker": "NIFTY 50",
        "shares": 75,
        "price": 62.0,
        "signal": "bullish trend read — bull call spread",
        "decision": decision,
        "why": "(headless proposal)",
        "spread": {"strategy": "bull_call_spread", "expiry": "2026-07-16",
                   "lot_size": 75, "lots": 1, "legs": []},
        "outcome": outcome,
    }


def _post_action(payload, headers=None):
    return _client().post("/api/discord/action", json=payload,
                          headers=headers or {})


# ------------------------------------------------------------ auth gate

def test_blocks_without_key():
    with _env_with_key():
        r = _post_action({"action": "approve", "trade_id": "pend0001"})
        assert r.status_code == 401
        assert r.json()["ok"] is False


def test_blocks_wrong_key():
    with _env_with_key():
        r = _post_action({"action": "approve", "trade_id": "pend0001"},
                         headers={"X-API-Key": "wrong-key"})
        assert r.status_code == 401


def test_fail_closed_when_key_unset():
    """Unlike src.api's dev mode, the gateway NEVER runs open."""
    with _env_without_key():
        r = _post_action({"action": "approve", "trade_id": "pend0001"})
        assert r.status_code == 503


def test_health_stays_public():
    with _env_with_key():
        r = _client().get("/api/health")
        assert r.status_code == 200
        assert r.json()["auth"] == "required"


def test_mounted_engine_routes_are_gated_too():
    """The whole surface (dashboard API included) sits behind the key."""
    with _env_with_key():
        r = _client().get("/api/watchlist")
        assert r.status_code == 401


# --------------------------------------------------- the Discord bridge

def test_approve_updates_pending_entry():
    entry = make_pending_entry()
    rewritten = {}
    with _env_with_key(), \
         mock.patch.object(op.journal, "read_all", return_value=[entry]), \
         mock.patch.object(op.journal, "rewrite_all",
                           side_effect=lambda e: rewritten.setdefault("v", e)), \
         mock.patch.object(op, "_notify_discord", return_value=True) as notif:
        r = _post_action({"action": "approve", "trade_id": "pend0001",
                          "why": "setup still valid"}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["decision"] == "approved"
    saved = rewritten["v"][0]
    assert saved["decision"] == "approved"
    assert saved["why"] == "setup still valid"
    assert notif.called  # decision confirmation pushed to Discord


def test_reject_updates_pending_entry():
    entry = make_pending_entry()
    rewritten = {}
    with _env_with_key(), \
         mock.patch.object(op.journal, "read_all", return_value=[entry]), \
         mock.patch.object(op.journal, "rewrite_all",
                           side_effect=lambda e: rewritten.setdefault("v", e)), \
         mock.patch.object(op, "_notify_discord", return_value=True):
        r = _post_action({"action": "reject", "trade_id": "pend0001"},
                         headers=AUTH)
    assert r.status_code == 200
    assert r.json()["decision"] == "rejected"
    saved = rewritten["v"][0]
    assert saved["decision"] == "rejected"
    assert saved["why"] == "(decided via Discord bridge)"


def test_unknown_trade_id_is_404():
    with _env_with_key(), \
         mock.patch.object(op.journal, "read_all",
                           return_value=[make_pending_entry("other001")]), \
         mock.patch.object(op.journal, "rewrite_all") as rw:
        r = _post_action({"action": "approve", "trade_id": "missing1"},
                         headers=AUTH)
    assert r.status_code == 404
    assert not rw.called


def test_non_pending_entry_is_404():
    """An already-decided entry is not decidable again by trade_id."""
    entry = make_pending_entry(decision="approved")
    with _env_with_key(), \
         mock.patch.object(op.journal, "read_all", return_value=[entry]), \
         mock.patch.object(op.journal, "rewrite_all") as rw:
        r = _post_action({"action": "reject", "trade_id": "pend0001"},
                         headers=AUTH)
    assert r.status_code == 404
    assert not rw.called


def test_already_resolved_entry_is_409_left_as_is():
    """Tracker resolved it hypothetically first — no hindsight approvals."""
    entry = make_pending_entry(outcome={"verdict": "MISSED GAIN"})
    with _env_with_key(), \
         mock.patch.object(op.journal, "read_all", return_value=[entry]), \
         mock.patch.object(op.journal, "rewrite_all") as rw:
        r = _post_action({"action": "approve", "trade_id": "pend0001"},
                         headers=AUTH)
    assert r.status_code == 409
    assert "MISSED GAIN" in r.json()["error"]
    assert not rw.called
    assert entry["decision"] == "pending_approval"  # untouched


def test_bad_action_is_400():
    with _env_with_key():
        r = _post_action({"action": "execute", "trade_id": "pend0001"},
                         headers=AUTH)
        assert r.status_code == 400


def test_missing_fields_rejected():
    with _env_with_key():
        r = _post_action({"action": "approve"}, headers=AUTH)
        assert r.status_code == 422  # pydantic validation


if __name__ == "__main__":
    test_blocks_without_key()
    test_blocks_wrong_key()
    test_fail_closed_when_key_unset()
    test_health_stays_public()
    test_mounted_engine_routes_are_gated_too()
    test_approve_updates_pending_entry()
    test_reject_updates_pending_entry()
    test_unknown_trade_id_is_404()
    test_non_pending_entry_is_404()
    test_already_resolved_entry_is_409_left_as_is()
    test_bad_action_is_400()
    test_missing_fields_rejected()
    print("All API-server (gateway) tests passed.")
