import asyncio
import sys
from pathlib import Path
from unittest import mock
from datetime import date

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Import the targets
from src.api import _poll_watchlist_loop, _notified_breaches, POLL_INTERVAL


def test_poll_watchlist_loop_one_cycle_no_breach():
    # Clear the notified breaches first
    _notified_breaches.clear()

    # Mock store.load_items
    mock_items = [
        {"ticker": "RELIANCE.NS", "condition": "percent_up", "value": 3.0}
    ]

    # Mock get_quote
    mock_quote = {
        "ticker": "RELIANCE.NS",
        "current_price": 2500.0,
        "prev_close": 2480.0,
        "percent_change": 0.81
    }

    sleep_count = 0
    async def custom_sleep(seconds):
        nonlocal sleep_count
        if seconds == POLL_INTERVAL:
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError()
        return None

    # Set up mocks
    with mock.patch("src.web.watchlist_store.load_items", return_value=mock_items), \
         mock.patch("src.api.get_quote", return_value=mock_quote), \
         mock.patch("src.api.check_rule", return_value=False) as mock_check_rule, \
         mock.patch("src.notifier.send_digest") as mock_send_digest, \
         mock.patch("asyncio.sleep", side_effect=custom_sleep):
        
        try:
            asyncio.run(_poll_watchlist_loop())
        except asyncio.CancelledError:
            pass

        # Assertions
        mock_check_rule.assert_called_once_with(mock_quote, "percent_up", 3.0)
        mock_send_digest.assert_not_called()
        assert len(_notified_breaches) == 0


def test_poll_watchlist_loop_one_cycle_with_breach_and_dedup():
    _notified_breaches.clear()

    mock_items = [
        {"ticker": "HDFCBANK.NS", "condition": "percent_up", "value": 3.0}
    ]

    mock_quote = {
        "ticker": "HDFCBANK.NS",
        "current_price": 1600.0,
        "prev_close": 1500.0,
        "percent_change": 6.67
    }

    # Cycle 1: Breach triggers and sends digest
    sleep_count_1 = 0
    async def custom_sleep_1(seconds):
        nonlocal sleep_count_1
        if seconds == POLL_INTERVAL:
            sleep_count_1 += 1
            if sleep_count_1 > 1:
                raise asyncio.CancelledError()
        return None

    with mock.patch("src.web.watchlist_store.load_items", return_value=mock_items), \
         mock.patch("src.api.get_quote", return_value=mock_quote), \
         mock.patch("src.api.check_rule", return_value=True), \
         mock.patch("src.api.describe", return_value="HDFCBANK.NS: percent_up 3.0%"), \
         mock.patch("src.notifier.send_digest") as mock_send_digest, \
         mock.patch("src.notifier.send_discord_message",
                    new_callable=mock.AsyncMock) as mock_discord, \
         mock.patch("asyncio.sleep", side_effect=custom_sleep_1):

        try:
            asyncio.run(_poll_watchlist_loop())
        except asyncio.CancelledError:
            pass

        # Verification for Cycle 1
        mock_send_digest.assert_called_once_with("ADiTrader Watchlist Alert", ["HDFCBANK.NS: percent_up 3.0%"])
        mock_discord.assert_awaited_once()
        assert "HDFCBANK.NS: percent_up 3.0%" in mock_discord.await_args.args[0]
        
        today_str = date.today().isoformat()
        expected_key = ("HDFCBANK.NS", "percent_up", 3.0, today_str)
        assert expected_key in _notified_breaches

    # Cycle 2: Breach still active, but should be deduplicated (no second digest sent)
    sleep_count_2 = 0
    async def custom_sleep_2(seconds):
        nonlocal sleep_count_2
        if seconds == POLL_INTERVAL:
            sleep_count_2 += 1
            if sleep_count_2 > 1:
                raise asyncio.CancelledError()
        return None

    with mock.patch("src.web.watchlist_store.load_items", return_value=mock_items), \
         mock.patch("src.api.get_quote", return_value=mock_quote), \
         mock.patch("src.api.check_rule", return_value=True), \
         mock.patch("src.api.describe", return_value="HDFCBANK.NS: percent_up 3.0%"), \
         mock.patch("src.notifier.send_digest") as mock_send_digest, \
         mock.patch("src.notifier.send_discord_message",
                    new_callable=mock.AsyncMock) as mock_discord, \
         mock.patch("asyncio.sleep", side_effect=custom_sleep_2):

        try:
            asyncio.run(_poll_watchlist_loop())
        except asyncio.CancelledError:
            pass

        # Verification for Cycle 2: nothing should be re-sent (email or Discord)
        mock_send_digest.assert_not_called()
        mock_discord.assert_not_awaited()


if __name__ == "__main__":
    test_poll_watchlist_loop_one_cycle_no_breach()
    test_poll_watchlist_loop_one_cycle_with_breach_and_dedup()
    print("All poll loop tests passed.")
