"""
Tests for the Discord episodic encoder path: the async webhook client
(src/discord_client.py), the notifier bridge (send_discord_message /
format_episode), and the pure Trade-Episode snapshot builder in
src/brain_map.py.

Offline — httpx network calls are mocked; no real webhook is ever hit.

Run:
    python tests/test_discord_notifier.py
    pytest tests/test_discord_notifier.py -v
"""

import asyncio
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map, notifier
from src import discord_client

FAKE_URL = "https://discord.com/api/webhooks/123/abc"


def _mock_async_client(status_code=204):
    """A stand-in for httpx.AsyncClient whose post() records its call and
    returns the given status."""
    response = mock.Mock(status_code=status_code)
    client = mock.AsyncMock()
    client.post = mock.AsyncMock(return_value=response)
    ctx = mock.AsyncMock()
    ctx.__aenter__.return_value = client
    ctx.__aexit__.return_value = False
    return ctx, client


def test_send_skips_when_webhook_unset():
    with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": ""}):
        with mock.patch("httpx.AsyncClient") as mock_client:
            ok = asyncio.run(discord_client.send_webhook_message("hello"))
    assert ok is False
    mock_client.assert_not_called()  # no network attempt at all


def test_send_skips_empty_content():
    with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": FAKE_URL}):
        with mock.patch("httpx.AsyncClient") as mock_client:
            ok = asyncio.run(discord_client.send_webhook_message(""))
    assert ok is False
    mock_client.assert_not_called()


def test_send_posts_content_to_webhook():
    ctx, client = _mock_async_client()
    with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": FAKE_URL}), \
         mock.patch("httpx.AsyncClient", return_value=ctx):
        ok = asyncio.run(discord_client.send_webhook_message("hello world"))
    assert ok is True
    args, kwargs = client.post.await_args
    assert args[0] == FAKE_URL
    assert kwargs["json"] == {"content": "hello world"}
    assert kwargs["params"] is None


def test_send_groups_by_thread_id():
    ctx, client = _mock_async_client()
    with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": FAKE_URL}), \
         mock.patch("httpx.AsyncClient", return_value=ctx):
        ok = asyncio.run(discord_client.send_webhook_message("msg", thread_id="999"))
    assert ok is True
    _, kwargs = client.post.await_args
    assert kwargs["params"] == {"thread_id": "999"}


def test_send_truncates_to_discord_limit():
    ctx, client = _mock_async_client()
    with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": FAKE_URL}), \
         mock.patch("httpx.AsyncClient", return_value=ctx):
        asyncio.run(discord_client.send_webhook_message("x" * 3000))
    _, kwargs = client.post.await_args
    assert len(kwargs["json"]["content"]) == discord_client.DISCORD_MESSAGE_LIMIT


def test_send_returns_false_on_http_error():
    ctx, _client = _mock_async_client(status_code=404)
    with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": FAKE_URL}), \
         mock.patch("httpx.AsyncClient", return_value=ctx):
        ok = asyncio.run(discord_client.send_webhook_message("msg"))
    assert ok is False


def test_send_swallows_network_exception():
    ctx, client = _mock_async_client()
    client.post.side_effect = ConnectionError("network down")
    with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": FAKE_URL}), \
         mock.patch("httpx.AsyncClient", return_value=ctx):
        ok = asyncio.run(discord_client.send_webhook_message("msg"))
    assert ok is False  # never raises


def test_notifier_delegates_to_webhook_client():
    with mock.patch("src.discord_client.send_webhook_message",
                    new_callable=mock.AsyncMock, return_value=True) as mock_send:
        ok = asyncio.run(notifier.send_discord_message("episode text", thread_id="42"))
    assert ok is True
    mock_send.assert_awaited_once_with("episode text", thread_id="42")


RESOLVED_ENTRY = {
    "short_id": "abcd1234",
    "date": "2026-07-01",
    "ticker": "TCS.NS",
    "action": "buy",
    "price": 4000.0,
    "shares": 5,
    "decision": "approved",
    "signal": "Fresh Golden Cross",
    "pattern_tags": ["Golden Cross", "Volume Spike"],
    "outcome": {
        "resolution": "target_hit",
        "price": 4400.0,
        "exit_date": "2026-07-05",
        "r_multiple": 2.0,
        "pnl_rs": 1950.0,
        "verdict": "good call",
    },
}

NEWS = {"tickers": {"TCS.NS": {"sentiment_score": 0.6,
                               "headline_focus": "strong earnings"}}}


def test_episode_snapshot_captures_context():
    ep = brain_map.build_episode_snapshot(RESOLVED_ENTRY, news=NEWS)
    assert ep["journal_ref"] == "abcd1234"
    assert ep["ticker"] == "TCS.NS"
    assert ep["entry_price"] == 4000.0 and ep["exit_price"] == 4400.0
    assert ep["resolution"] == "target_hit"
    assert ep["rules_breached"] == ["target_hit"]
    assert ep["r_multiple"] == 2.0
    assert ep["market_sentiment"] == {"score": 0.6,
                                      "headline_focus": "strong earnings"}
    assert ep["pattern_tags"] == ["Golden Cross", "Volume Spike"]


def test_episode_snapshot_none_for_unresolved_entry():
    open_entry = dict(RESOLVED_ENTRY, outcome=None)
    assert brain_map.build_episode_snapshot(open_entry, news=NEWS) is None


def test_episode_snapshot_without_news_coverage():
    ep = brain_map.build_episode_snapshot(RESOLVED_ENTRY, news={})
    assert ep["market_sentiment"] == {"score": None, "headline_focus": None}


def test_mock_trade_is_a_pure_dry_run():
    """--mock-trade-strategy sends a [MOCK] episode through the notifier
    and never touches the journal (it has no journal write path at all)."""
    import src.plan_tracker as plan_tracker
    with mock.patch("src.notifier.send_discord_message",
                    new_callable=mock.AsyncMock, return_value=True) as mock_send, \
         mock.patch.object(plan_tracker.journal, "log",
                           side_effect=AssertionError("mock trade must not journal")), \
         mock.patch.object(plan_tracker.journal, "rewrite_all",
                           side_effect=AssertionError("mock trade must not journal")):
        assert plan_tracker.run_mock_trade("IRON_BUTTERFLY") is True
        sent = mock_send.await_args.args[0]
        assert "MOCK" in sent and "iron_butterfly" in sent
        assert plan_tracker.run_mock_trade("IRON_CONDOR") is True
        assert "iron_condor" in mock_send.await_args.args[0]


def test_format_episode_is_structured_message():
    ep = brain_map.build_episode_snapshot(RESOLVED_ENTRY, news=NEWS)
    text = notifier.format_episode(ep)
    assert "Trade Episode — TCS.NS" in text
    assert "target_hit" in text
    assert "4000.0" in text and "4400.0" in text
    assert "Golden Cross, Volume Spike" in text
    assert "+0.60" in text and "strong earnings" in text
    assert len(text) <= discord_client.DISCORD_MESSAGE_LIMIT


if __name__ == "__main__":
    test_mock_trade_is_a_pure_dry_run()
    test_send_skips_when_webhook_unset()
    test_send_skips_empty_content()
    test_send_posts_content_to_webhook()
    test_send_groups_by_thread_id()
    test_send_truncates_to_discord_limit()
    test_send_returns_false_on_http_error()
    test_send_swallows_network_exception()
    test_notifier_delegates_to_webhook_client()
    test_episode_snapshot_captures_context()
    test_episode_snapshot_none_for_unresolved_entry()
    test_episode_snapshot_without_news_coverage()
    test_format_episode_is_structured_message()
    print("13/13 Discord notifier tests passed.")
