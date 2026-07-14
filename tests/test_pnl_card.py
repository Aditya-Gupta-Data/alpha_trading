"""
Tests for the on-demand P&L card (owner ask 2026-07-14: realized + live
marked, on Discord). Offline — injected summary/marks; the gateway
endpoint tested through the api_server test client pattern.

    python -m pytest tests/test_pnl_card.py
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import portfolio_report as pr

SUMMARY = {"starting_capital": 1000000.0, "realized_pnl": 12500.50,
           "equity": 1012500.50, "available_cash": 750000.0,
           "locked_margin": 262500.5, "open_locks": 12,
           "trading_halted": False}
MARKS = [
    {"short_id": "a1", "ticker": "NIFTY BANK", "strategy": "bear_put_spread",
     "live_pnl_rs": 8737.75, "detail": "100% of max profit"},
    {"short_id": "b2", "ticker": "NIFTY 50", "strategy": "bear_put_spread",
     "live_pnl_rs": -2692.88, "detail": "-32% of max profit"},
]
NOW = datetime(2026, 7, 14, 18, 30)


def test_card_shows_realized_live_and_total():
    card = pr.build_pnl_card(summary=SUMMARY, marked=MARKS,
                             mark_source="engine_snapshot", now=NOW)
    assert card["realized_pnl"] == 12500.50
    assert abs(card["live_net"] - 6044.87) < 0.01
    assert abs(card["total"] - 18545.37) < 0.01
    text = card["text"]
    assert "Realized (banked): Rs.+12,500.50" in text
    assert "Live marked (2 open)" in text and "engine snapshot" in text
    assert "Total if all closed at marks" in text
    assert "Best: NIFTY BANK" in text and "Worst: NIFTY 50" in text
    assert "2026-07-14 18:30 IST" in text


def test_card_honest_when_no_marks():
    card = pr.build_pnl_card(summary=SUMMARY, marked=[], mark_source=None,
                             now=NOW)
    assert card["live_net"] is None
    assert card["total"] == 12500.50          # realized alone
    assert "no marks available" in card["text"]
    assert "Realized (banked)" in card["text"]


def test_card_survives_broken_account_layer():
    card = pr.build_pnl_card(summary={"error": "db gone"}, marked=MARKS,
                             mark_source="direct_fetch", now=NOW)
    assert card["realized_pnl"] is None
    assert "Realized: unavailable" in card["text"]
    assert card["live_net"] is not None       # marks still shown


def test_report_payload_carries_realized_field():
    exposure = {"locked_margin_rs": 260130.25, "equity_rs": 1000000.0,
                "realized_pnl_rs": 4321.0, "exposure_pct": 26.01}
    payload = pr.build_report_payload(MARKS, open_count=13, unmarked=0,
                                      exposure=exposure, now=NOW)
    names = [f["name"] for f in payload["fields"]]
    assert "Realized P&L (banked)" in names
    realized_field = next(f for f in payload["fields"]
                          if f["name"] == "Realized P&L (banked)")
    assert "4,321" in realized_field["value"]
    # Old exposure field intact.
    assert any("Exposure" == f["name"] for f in payload["fields"])


def test_gateway_pnl_endpoint():
    import os
    os.environ.setdefault("API_KEY", "test-key-123")
    from fastapi.testclient import TestClient
    from src import api_server
    client = TestClient(api_server.app)
    # No key -> refused (fail-closed gateway).
    assert client.get("/api/discord/pnl").status_code in (401, 403, 503)
    r = client.get("/api/discord/pnl",
                   headers={"x-api-key": os.environ["API_KEY"]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "text" in body["card"]
    assert "P&L" in body["card"]["text"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
