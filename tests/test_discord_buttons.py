"""
Tests for the Discord bot's Phase 9 bridge client (src/discord_bot.py):
the /pending helpers that fetch pending proposals from the gateway and
POST decisions back — all over authenticated HTTP, never touching the
journal or engine modules directly.

Offline — urllib is mocked; no Discord connection, no gateway, no network.
The discord.ui classes themselves (buttons/modal) are exercised only at
the pure/format level; the interactive machinery is Discord's.

Run:
    python tests/test_discord_buttons.py
    pytest tests/test_discord_buttons.py -v
"""

import io
import json
import os
import re
import sys
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import discord_bot as bot


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _pending_item(trade_id="pend0001"):
    return {"trade_id": trade_id, "ticker": "NIFTY 50",
            "strategy": "bull_call_spread", "expiry": "2026-07-16",
            "proposed_on": "2026-07-07", "lots": 1, "lot_size": 75,
            "net_kind": "debit", "net_per_share": 62.0,
            "max_loss": 4650.0, "max_profit": 10350.0,
            "signal": "bullish trend read"}


# ------------------------------------------------------------ bridge calls

def test_bridge_call_sends_key_and_parses_json():
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        captured["key"] = req.get_header("X-api-key")
        captured["method"] = req.get_method()
        return FakeResponse(200, {"ok": True, "pending": []})

    with mock.patch.dict(os.environ, {"API_KEY": "test-key"}, clear=False), \
         mock.patch.object(bot.urllib.request, "urlopen", fake_urlopen):
        status, body = bot._bridge_call("GET", "/api/discord/pending")
    assert status == 200 and body == {"ok": True, "pending": []}
    assert captured["key"] == "test-key"
    assert captured["url"].endswith("/api/discord/pending")
    assert captured["method"] == "GET"


def test_bridge_call_returns_http_error_bodies_instead_of_raising():
    err = urllib.error.HTTPError(
        url="http://x", code=404, msg="nf", hdrs=None,
        fp=io.BytesIO(json.dumps({"ok": False, "error": "no entry"}).encode()))
    with mock.patch.object(bot.urllib.request, "urlopen", side_effect=err):
        status, body = bot._bridge_call("POST", "/api/discord/action",
                                        {"action": "approve", "trade_id": "x"})
    assert status == 404 and body["error"] == "no entry"


def test_bridge_base_url_env_override():
    with mock.patch.dict(os.environ, {"BRIDGE_BASE_URL": "http://vm:9000/"},
                         clear=False):
        assert bot._bridge_base_url() == "http://vm:9000"


def test_fetch_pending_unwraps_list():
    with mock.patch.object(bot, "_bridge_call",
                           return_value=(200, {"ok": True,
                                               "pending": [_pending_item()]})):
        items = bot._fetch_pending()
    assert len(items) == 1 and items[0]["trade_id"] == "pend0001"


def test_fetch_pending_raises_on_gateway_error():
    with mock.patch.object(bot, "_bridge_call",
                           return_value=(503, {"ok": False, "error": "locked"})):
        try:
            bot._fetch_pending()
            assert False, "should have raised"
        except RuntimeError as e:
            assert "locked" in str(e)


def test_decide_posts_action_payload():
    with mock.patch.object(bot, "_bridge_call",
                           return_value=(200, {"ok": True,
                                               "decision": "approved"})) as call:
        status, body = bot._decide("pend0001", "approve", "setup still valid")
    assert status == 200 and body["decision"] == "approved"
    call.assert_called_once_with("POST", "/api/discord/action",
                                 {"action": "approve", "trade_id": "pend0001",
                                  "why": "setup still valid"})


# --------------------------------------------------------- formatting / ids

def test_format_pending_carries_key_facts():
    text = bot._format_pending(_pending_item())
    assert "Bull Call Spread" in text and "NIFTY 50" in text
    assert "`pend0001`" in text and "2026-07-16" in text
    assert "max loss Rs.4,650" in text and "paper only" in text
    assert len(text) <= bot.DISCORD_MSG_LIMIT


def test_custom_id_roundtrips_through_template():
    cid = bot._custom_id("approve", "pend0001")
    match = re.fullmatch(bot._DECISION_TEMPLATE, cid)
    assert match and match["action"] == "approve"
    assert match["trade_id"] == "pend0001"
    assert re.fullmatch(bot._DECISION_TEMPLATE,
                        bot._custom_id("reject", "ab12cd34"))["action"] == "reject"


def test_pending_view_has_both_buttons():
    view = bot._pending_view("pend0001")
    ids = sorted(item.custom_id for item in view.children)
    assert ids == ["adit:approve:pend0001", "adit:reject:pend0001"]
    assert view.timeout is None  # buttons must not expire after 180s


if __name__ == "__main__":
    test_bridge_call_sends_key_and_parses_json()
    test_bridge_call_returns_http_error_bodies_instead_of_raising()
    test_bridge_base_url_env_override()
    test_fetch_pending_unwraps_list()
    test_fetch_pending_raises_on_gateway_error()
    test_decide_posts_action_payload()
    test_format_pending_carries_key_facts()
    test_custom_id_roundtrips_through_template()
    test_pending_view_has_both_buttons()
    print("All Discord button/bridge-client tests passed.")
