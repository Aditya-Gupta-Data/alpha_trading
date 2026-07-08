"""
Tests for broadcast_alert (structured embed dispatcher), fire_broadcast
(sync bridge), and eod_summary query/card helpers. All offline — httpx
network calls are mocked; no real webhook is ever hit. Uses pytest-mock's
mocker fixture for concise patching alongside unittest.mock for env-var
context managers.

Run:
    pytest tests/test_notifier.py -v
    python tests/test_notifier.py
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import notifier
from src.notifier import (
    _COLOUR,
    _COLOUR_LOSS,
    _COLOUR_WIN,
    _build_embed,
    _embed_colour,
    broadcast_alert,
    fire_broadcast,
)

FAKE_URL = "https://discord.com/api/webhooks/123/abc"


@pytest.fixture(autouse=True)
def _unmuzzled_webhooks(monkeypatch):
    """These tests exercise the dispatch machinery itself, so the Phase 6J
    test-environment muzzle is explicitly disabled (every network call is
    mocked anyway). The muzzle's own behavior is tested separately below —
    those tests re-enable it locally."""
    monkeypatch.setattr(notifier, "WEBHOOK_MUZZLE_OVERRIDE", False)

# ---------------------------------------------------------------------------
# Shared test payloads
# ---------------------------------------------------------------------------

OPENED_PAYLOAD = {
    "event":      "opened",
    "ticker":     "NIFTY 50",
    "date":       "2026-07-08",
    "strategy":   "iron_condor",
    "short_id":   "abc12345",
    "lots":       1,
    "lot_size":   75,
    "max_loss":   5250.0,
    "max_profit": 4750.0,
    "expiry":     "2026-07-25",
    "signal":     "Neutral momentum, VIX calm",
}

CLOSED_WIN_PAYLOAD = {
    "event":        "closed",
    "ticker":       "TCS.NS",
    "date":         "2026-07-08",
    "strategy":     "bull_call_spread",
    "short_id":     "def67890",
    "resolution":   "target_hit",
    "pnl_rs":       1850.0,
    "r_multiple":   1.5,
    "verdict":      "WIN — target hit",
    "days_in_trade": 7,
    "frictions_rs": 42.50,
}

CLOSED_LOSS_PAYLOAD = {
    "event":        "closed",
    "ticker":       "INFY.NS",
    "date":         "2026-07-08",
    "resolution":   "pre_expiry_exit",
    "pnl_rs":       -800.0,
    "r_multiple":   -0.4,
    "verdict":      "LOSS — closed behind at the pre-expiry exit",
    "days_in_trade": 5,
    "frictions_rs": 35.00,
}

STOP_LOSS_PAYLOAD = {
    "event":        "stop_loss",
    "ticker":       "HDFCBANK.NS",
    "date":         "2026-07-08",
    "short_id":     "ghi11223",
    "resolution":   "stop_hit",
    "pnl_rs":       -2100.0,
    "r_multiple":   -1.0,
    "verdict":      "LOSS — stop hit",
    "days_in_trade": 3,
    "frictions_rs": 38.00,
}

EOD_PAYLOAD = {
    "event":       "eod",
    "ticker":      "",
    "date":        "2026-07-08",
    "description": "Market closed. All positions reviewed.",
    "fields": [
        {"name": "Active Spreads", "value": "2",          "inline": True},
        {"name": "MTM P&L",        "value": "Rs.+1,200",  "inline": True},
    ],
}


# ---------------------------------------------------------------------------
# Helper: mock httpx.AsyncClient
# ---------------------------------------------------------------------------

def _mock_httpx_client(status_code: int = 204):
    response = mock.Mock(status_code=status_code)
    client   = mock.AsyncMock()
    client.post = mock.AsyncMock(return_value=response)
    ctx = mock.AsyncMock()
    ctx.__aenter__.return_value = client
    ctx.__aexit__.return_value  = False
    return ctx, client


# ---------------------------------------------------------------------------
# _build_embed structure — opened
# ---------------------------------------------------------------------------

def test_build_embed_opened_title_contains_ticker():
    embed = _build_embed(OPENED_PAYLOAD)
    assert "NIFTY 50" in embed["title"]


def test_build_embed_opened_title_says_trade_opened():
    embed = _build_embed(OPENED_PAYLOAD)
    assert "Trade Opened" in embed["title"] or "Opened" in embed["title"]


def test_build_embed_opened_has_max_loss_and_max_profit_fields():
    embed  = _build_embed(OPENED_PAYLOAD)
    names  = [f["name"] for f in embed["fields"]]
    assert "Max Loss" in names
    assert "Max Profit" in names


def test_build_embed_opened_max_loss_value():
    embed = _build_embed(OPENED_PAYLOAD)
    field = next(f for f in embed["fields"] if f["name"] == "Max Loss")
    assert "5,250" in field["value"]


def test_build_embed_opened_includes_trade_id_field():
    embed = _build_embed(OPENED_PAYLOAD)
    names = [f["name"] for f in embed["fields"]]
    assert "Trade ID" in names
    tid   = next(f for f in embed["fields"] if f["name"] == "Trade ID")
    assert "abc12345" in tid["value"]


def test_build_embed_opened_includes_signal_field():
    embed = _build_embed(OPENED_PAYLOAD)
    names = [f["name"] for f in embed["fields"]]
    assert "Signal" in names


def test_build_embed_opened_omits_signal_when_empty():
    payload = dict(OPENED_PAYLOAD, signal="")
    embed   = _build_embed(payload)
    names   = [f["name"] for f in embed["fields"]]
    assert "Signal" not in names


# ---------------------------------------------------------------------------
# _build_embed structure — closed / stop_loss
# ---------------------------------------------------------------------------

def test_build_embed_closed_has_pnl_field():
    embed = _build_embed(CLOSED_WIN_PAYLOAD)
    names = [f["name"] for f in embed["fields"]]
    assert "P&L" in names


def test_build_embed_closed_pnl_value_positive():
    embed  = _build_embed(CLOSED_WIN_PAYLOAD)
    field  = next(f for f in embed["fields"] if f["name"] == "P&L")
    assert "1,850" in field["value"]
    assert "+" in field["value"]


def test_build_embed_closed_includes_verdict():
    embed  = _build_embed(CLOSED_WIN_PAYLOAD)
    names  = [f["name"] for f in embed["fields"]]
    assert "Verdict" in names
    vfield = next(f for f in embed["fields"] if f["name"] == "Verdict")
    assert "WIN" in vfield["value"]


def test_build_embed_closed_includes_frictions():
    embed = _build_embed(CLOSED_WIN_PAYLOAD)
    names = [f["name"] for f in embed["fields"]]
    assert "Frictions" in names


def test_build_embed_stop_loss_title_contains_ticker():
    embed = _build_embed(STOP_LOSS_PAYLOAD)
    assert "HDFCBANK.NS" in embed["title"]


def test_build_embed_stop_loss_title_signals_stop():
    embed  = _build_embed(STOP_LOSS_PAYLOAD)
    title  = embed["title"].lower()
    assert "stop" in title or "loss" in title


# ---------------------------------------------------------------------------
# _build_embed structure — eod
# ---------------------------------------------------------------------------

def test_build_embed_eod_title_says_summary():
    embed = _build_embed(EOD_PAYLOAD)
    assert "Summary" in embed["title"] or "End-of-Day" in embed["title"]


def test_build_embed_eod_uses_pre_built_fields():
    embed = _build_embed(EOD_PAYLOAD)
    assert embed["fields"] == EOD_PAYLOAD["fields"]


def test_build_embed_eod_does_not_overwrite_fields():
    payload = dict(EOD_PAYLOAD, fields=[{"name": "X", "value": "Y", "inline": True}])
    embed   = _build_embed(payload)
    assert embed["fields"] == [{"name": "X", "value": "Y", "inline": True}]


# ---------------------------------------------------------------------------
# _build_embed — common
# ---------------------------------------------------------------------------

def test_build_embed_description_included_when_present():
    embed = _build_embed(EOD_PAYLOAD)
    assert embed.get("description") == "Market closed. All positions reviewed."


def test_build_embed_no_description_key_when_absent():
    payload = dict(OPENED_PAYLOAD)
    payload.pop("description", None)
    embed   = _build_embed(payload)
    assert "description" not in embed


def test_build_embed_has_footer_with_date():
    embed = _build_embed(OPENED_PAYLOAD)
    assert "footer" in embed
    assert "2026-07-08" in embed["footer"]["text"]


def test_build_embed_has_color_field():
    embed = _build_embed(OPENED_PAYLOAD)
    assert "color" in embed
    assert isinstance(embed["color"], int)


# ---------------------------------------------------------------------------
# _embed_colour
# ---------------------------------------------------------------------------

def test_embed_colour_opened_is_green():
    assert _embed_colour(OPENED_PAYLOAD) == _COLOUR["opened"]


def test_embed_colour_eod_is_blue():
    assert _embed_colour(EOD_PAYLOAD) == _COLOUR["eod"]


def test_embed_colour_stop_loss_is_red():
    assert _embed_colour(STOP_LOSS_PAYLOAD) == _COLOUR["stop_loss"]


def test_embed_colour_closed_win_is_green():
    assert _embed_colour(CLOSED_WIN_PAYLOAD) == _COLOUR_WIN


def test_embed_colour_closed_loss_is_red():
    assert _embed_colour(CLOSED_LOSS_PAYLOAD) == _COLOUR_LOSS


def test_embed_colour_unknown_event_is_grey():
    payload = {"event": "unknown_xyz", "ticker": "X", "date": "2026-07-08"}
    assert _embed_colour(payload) == 0x95A5A6


def test_embed_colour_closed_no_verdict_uses_orange():
    payload = {"event": "closed", "ticker": "X", "date": "2026-07-08"}
    assert _embed_colour(payload) == _COLOUR["closed"]


# ---------------------------------------------------------------------------
# broadcast_alert — network tests
# ---------------------------------------------------------------------------

def test_broadcast_alert_returns_false_when_webhook_unset():
    with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": ""}):
        ok = asyncio.run(broadcast_alert(OPENED_PAYLOAD))
    assert ok is False


def test_broadcast_alert_posts_embed_body(mocker):
    ctx, client = _mock_httpx_client(status_code=204)
    mocker.patch("httpx.AsyncClient", return_value=ctx)
    with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": FAKE_URL}):
        ok = asyncio.run(broadcast_alert(OPENED_PAYLOAD))
    assert ok is True
    _, kwargs = client.post.await_args
    assert "embeds" in kwargs["json"]
    assert isinstance(kwargs["json"]["embeds"], list)
    assert len(kwargs["json"]["embeds"]) == 1


def test_broadcast_alert_embed_title_in_request(mocker):
    ctx, client = _mock_httpx_client()
    mocker.patch("httpx.AsyncClient", return_value=ctx)
    with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": FAKE_URL}):
        asyncio.run(broadcast_alert(OPENED_PAYLOAD))
    _, kwargs = client.post.await_args
    embed = kwargs["json"]["embeds"][0]
    assert "NIFTY 50" in embed["title"]


def test_broadcast_alert_embed_colour_in_request(mocker):
    ctx, client = _mock_httpx_client()
    mocker.patch("httpx.AsyncClient", return_value=ctx)
    with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": FAKE_URL}):
        asyncio.run(broadcast_alert(STOP_LOSS_PAYLOAD))
    _, kwargs = client.post.await_args
    embed = kwargs["json"]["embeds"][0]
    assert embed["color"] == _COLOUR["stop_loss"]


def test_broadcast_alert_returns_false_on_http_error(mocker):
    ctx, _ = _mock_httpx_client(status_code=400)
    mocker.patch("httpx.AsyncClient", return_value=ctx)
    with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": FAKE_URL}):
        ok = asyncio.run(broadcast_alert(STOP_LOSS_PAYLOAD))
    assert ok is False


def test_broadcast_alert_swallows_network_exception(mocker):
    ctx, client = _mock_httpx_client()
    client.post.side_effect = ConnectionError("network down")
    mocker.patch("httpx.AsyncClient", return_value=ctx)
    with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": FAKE_URL}):
        ok = asyncio.run(broadcast_alert(OPENED_PAYLOAD))
    assert ok is False   # never raises


def test_broadcast_alert_does_not_use_content_key(mocker):
    ctx, client = _mock_httpx_client()
    mocker.patch("httpx.AsyncClient", return_value=ctx)
    with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": FAKE_URL}):
        asyncio.run(broadcast_alert(CLOSED_WIN_PAYLOAD))
    _, kwargs = client.post.await_args
    assert "content" not in kwargs["json"]


# ---------------------------------------------------------------------------
# fire_broadcast — sync bridge
# ---------------------------------------------------------------------------

def test_fire_broadcast_calls_broadcast_alert(mocker):
    captured = {}

    async def fake_broadcast(payload):
        captured["payload"] = payload
        return True

    mocker.patch("src.notifier.broadcast_alert", new=fake_broadcast)
    fire_broadcast(OPENED_PAYLOAD)
    assert captured.get("payload") == OPENED_PAYLOAD


def test_fire_broadcast_never_raises_on_broadcast_failure(mocker):
    async def broken(_payload):
        raise RuntimeError("simulated network failure")

    mocker.patch("src.notifier.broadcast_alert", new=broken)
    fire_broadcast(OPENED_PAYLOAD)   # must not raise


def test_fire_broadcast_swallows_import_errors(mocker):
    mocker.patch(
        "src.notifier.broadcast_alert",
        side_effect=ImportError("httpx not found"),
    )
    fire_broadcast(OPENED_PAYLOAD)   # must not raise


# ---------------------------------------------------------------------------
# eod_summary — query_todays_resolutions
# ---------------------------------------------------------------------------

def test_eod_query_returns_empty_for_missing_db():
    from src.eod_summary import query_todays_resolutions
    result = query_todays_resolutions(db_path="/nonexistent/path/brain_map.db")
    assert result == []


def test_eod_query_returns_todays_rows():
    from src.eod_summary import query_todays_resolutions
    today = "2026-07-08"
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE outcomes (
                id INTEGER PRIMARY KEY, journal_ref TEXT, date TEXT,
                ticker TEXT, archetype TEXT, r_multiple REAL,
                result TEXT, post_mortem TEXT
            )""")
        conn.execute(
            "INSERT INTO outcomes VALUES (1,'ref1',?,?,NULL,1.5,'win',NULL)",
            (today, "NIFTY 50"))
        conn.execute(
            "INSERT INTO outcomes VALUES (2,'ref2','2026-07-07','TCS.NS',NULL,-1.0,'loss',NULL)")
        conn.commit()
        conn.close()
        rows = query_todays_resolutions(db_path=db_path)
        assert len(rows) == 1
        assert rows[0]["ticker"] == "NIFTY 50"
        assert rows[0]["result"] == "win"
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_eod_query_handles_corrupt_db_gracefully():
    from src.eod_summary import query_todays_resolutions
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        f.write(b"not a valid sqlite file")
        db_path = f.name
    try:
        # Must not raise
        result = query_todays_resolutions(db_path=db_path)
        assert isinstance(result, list)
    finally:
        Path(db_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# eod_summary — compute_net_delta_exposure
# ---------------------------------------------------------------------------

def test_eod_net_delta_empty_spreads():
    from src.eod_summary import compute_net_delta_exposure
    assert compute_net_delta_exposure([]) == 0.0


def test_eod_net_delta_iron_condor_is_zero():
    from src.eod_summary import compute_net_delta_exposure
    spreads = [{"spread": {
        "strategy": "iron_condor", "lots": 1, "lot_size": 75,
    }}]
    assert compute_net_delta_exposure(spreads) == 0.0


def test_eod_net_delta_iron_butterfly_is_zero():
    from src.eod_summary import compute_net_delta_exposure
    spreads = [{"spread": {
        "strategy": "iron_butterfly", "lots": 1, "lot_size": 75,
    }}]
    assert compute_net_delta_exposure(spreads) == 0.0


def test_eod_net_delta_bull_call_spread_is_positive():
    from src.eod_summary import compute_net_delta_exposure
    spreads = [{"spread": {
        "strategy": "bull_call_spread", "lots": 1, "lot_size": 75,
    }}]
    delta = compute_net_delta_exposure(spreads)
    assert delta > 0


def test_eod_net_delta_bear_put_spread_is_negative():
    from src.eod_summary import compute_net_delta_exposure
    spreads = [{"spread": {
        "strategy": "bear_put_spread", "lots": 1, "lot_size": 75,
    }}]
    delta = compute_net_delta_exposure(spreads)
    assert delta < 0


def test_eod_net_delta_scales_with_lots_and_lot_size():
    from src.eod_summary import compute_net_delta_exposure
    spreads = [{"spread": {
        "strategy": "bull_call_spread", "lots": 2, "lot_size": 50,
    }}]
    delta = compute_net_delta_exposure(spreads)
    # bias=1.0 * ATM_DELTA=0.5 * lots=2 * lot_size=50 = 50.0
    assert delta == pytest.approx(50.0)


def test_eod_net_delta_unknown_strategy_is_zero():
    from src.eod_summary import compute_net_delta_exposure
    spreads = [{"spread": {"strategy": "mystery_spread", "lots": 1, "lot_size": 75}}]
    assert compute_net_delta_exposure(spreads) == 0.0


# ---------------------------------------------------------------------------
# eod_summary — build_eod_card
# ---------------------------------------------------------------------------

def test_eod_build_card_event_type_is_eod(mocker):
    from src.eod_summary import build_eod_card
    mocker.patch("src.eod_summary._read_journal", return_value=[])
    mocker.patch("src.eod_summary.query_todays_resolutions", return_value=[])
    card = build_eod_card()
    assert card["event"] == "eod"


def test_eod_build_card_has_date(mocker):
    from src.eod_summary import build_eod_card
    mocker.patch("src.eod_summary._read_journal", return_value=[])
    mocker.patch("src.eod_summary.query_todays_resolutions", return_value=[])
    card = build_eod_card()
    assert "date" in card and len(card["date"]) == 10


def test_eod_build_card_has_fields_list(mocker):
    from src.eod_summary import build_eod_card
    mocker.patch("src.eod_summary._read_journal", return_value=[])
    mocker.patch("src.eod_summary.query_todays_resolutions", return_value=[])
    card = build_eod_card()
    assert isinstance(card.get("fields"), list)
    assert len(card["fields"]) > 0


def test_eod_build_card_shows_active_spread_count(mocker):
    from src.eod_summary import build_eod_card
    spread_entry = {
        "decision": "approved",
        "outcome":  None,
        "spread": {
            "strategy": "iron_condor", "lots": 1, "lot_size": 75,
            "legs": [], "expiry": "2026-07-25",
            "max_loss": 5000, "max_profit": 4000,
        },
    }
    mocker.patch("src.eod_summary._read_journal", return_value=[spread_entry])
    mocker.patch("src.eod_summary.query_todays_resolutions", return_value=[])
    card = build_eod_card()
    field_map = {f["name"]: f["value"] for f in card["fields"]}
    assert "Active Spreads" in field_map
    assert field_map["Active Spreads"] == "1"


def test_eod_build_card_shows_net_delta_field(mocker):
    from src.eod_summary import build_eod_card
    spread_entry = {
        "decision": "approved",
        "outcome":  None,
        "spread": {
            "strategy": "bull_call_spread", "lots": 1, "lot_size": 75,
        },
    }
    mocker.patch("src.eod_summary._read_journal", return_value=[spread_entry])
    mocker.patch("src.eod_summary.query_todays_resolutions", return_value=[])
    card = build_eod_card()
    field_names = [f["name"] for f in card["fields"]]
    assert "Net Delta" in field_names


def test_eod_build_card_idle_description_when_no_positions(mocker):
    from src.eod_summary import build_eod_card
    mocker.patch("src.eod_summary._read_journal", return_value=[])
    mocker.patch("src.eod_summary.query_todays_resolutions", return_value=[])
    card = build_eod_card()
    assert "idle" in card.get("description", "").lower()


# ---------------------------------------------------------------------------
# Phase 6J: the test-environment webhook muzzle
# ---------------------------------------------------------------------------

def _re_arm_muzzle(monkeypatch):
    """Undo this file's autouse unmuzzle so the real env checks run."""
    monkeypatch.setattr(notifier, "WEBHOOK_MUZZLE_OVERRIDE", None)


def test_muzzle_engages_on_the_is_test_env_flag(monkeypatch):
    _re_arm_muzzle(monkeypatch)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    for truthy in ("True", "true", "1", "yes"):
        monkeypatch.setenv("IS_TEST_ENV", truthy)
        assert notifier.webhooks_muzzled() is True
    monkeypatch.setenv("IS_TEST_ENV", "False")
    assert notifier.webhooks_muzzled() is False
    monkeypatch.delenv("IS_TEST_ENV")
    assert notifier.webhooks_muzzled() is False


def test_muzzle_auto_detects_a_pytest_run(monkeypatch):
    _re_arm_muzzle(monkeypatch)
    monkeypatch.delenv("IS_TEST_ENV", raising=False)
    # pytest sets PYTEST_CURRENT_TEST for the duration of every test —
    # the muzzle must engage on that alone, with no flag configured
    assert os.environ.get("PYTEST_CURRENT_TEST")
    assert notifier.webhooks_muzzled() is True


def test_muzzled_broadcast_never_touches_the_network(monkeypatch, mocker):
    _re_arm_muzzle(monkeypatch)
    monkeypatch.setenv("IS_TEST_ENV", "True")
    tripwire = mocker.patch("httpx.AsyncClient",
                            side_effect=AssertionError("HTTP escaped the muzzle!"))
    mocker.patch("src.discord_client._webhook_url", return_value=FAKE_URL)
    result = asyncio.run(broadcast_alert(dict(OPENED_PAYLOAD)))
    assert result is False          # reported as not-delivered
    tripwire.assert_not_called()    # no client was ever constructed


def test_muzzled_discord_message_never_reaches_the_webhook_client(
        monkeypatch, mocker):
    _re_arm_muzzle(monkeypatch)
    monkeypatch.setenv("IS_TEST_ENV", "True")
    tripwire = mocker.patch("src.discord_client.send_webhook_message",
                            side_effect=AssertionError("HTTP escaped the muzzle!"))
    result = asyncio.run(notifier.send_discord_message("live alert text"))
    assert result is False
    tripwire.assert_not_called()


def test_muzzled_send_logs_locally(monkeypatch, capsys):
    _re_arm_muzzle(monkeypatch)
    monkeypatch.setenv("IS_TEST_ENV", "True")
    asyncio.run(notifier.send_discord_message("hello from the sandbox"))
    out = capsys.readouterr().out
    assert "muzzled" in out and "hello from the sandbox" in out


def test_unmuzzled_dispatch_still_works(mocker):
    # the autouse fixture has WEBHOOK_MUZZLE_OVERRIDE = False here — a
    # true live run must be able to deliver exactly as before
    mocker.patch("src.discord_client._webhook_url", return_value=FAKE_URL)
    fake_resp = mock.Mock(status_code=204)
    fake_client = mock.AsyncMock()
    fake_client.__aenter__.return_value.post = mock.AsyncMock(
        return_value=fake_resp)
    mocker.patch("httpx.AsyncClient", return_value=fake_client)
    assert asyncio.run(broadcast_alert(dict(OPENED_PAYLOAD))) is True


if __name__ == "__main__":
    # Quick smoke-run outside pytest (no mocker fixture — skips those tests).
    test_build_embed_opened_title_contains_ticker()
    test_build_embed_opened_has_max_loss_and_max_profit_fields()
    test_build_embed_opened_includes_trade_id_field()
    test_build_embed_closed_has_pnl_field()
    test_build_embed_closed_includes_verdict()
    test_build_embed_eod_uses_pre_built_fields()
    test_build_embed_has_footer_with_date()
    test_embed_colour_opened_is_green()
    test_embed_colour_closed_win_is_green()
    test_embed_colour_closed_loss_is_red()
    test_embed_colour_stop_loss_is_red()
    test_embed_colour_eod_is_blue()
    test_embed_colour_unknown_event_is_grey()
    test_broadcast_alert_returns_false_when_webhook_unset()
    test_eod_query_returns_empty_for_missing_db()
    test_eod_query_returns_todays_rows()
    test_eod_net_delta_empty_spreads()
    test_eod_net_delta_iron_condor_is_zero()
    test_eod_net_delta_bull_call_spread_is_positive()
    test_eod_net_delta_bear_put_spread_is_negative()
    print("Standalone tests passed (mocker-dependent tests skipped).")
