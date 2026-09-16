"""
RULE 6 muzzles at the network doors (2026-09-16, ledger Issue 29).

The suite had been "hermetic" only because Dhan and Gemini answered fast:
five tests dialled them for real through un-stubbed defaults. The night
TCP connects to api.dhan.co hung, those tests took 8 minutes each and the
suite went from 4 minutes to 24. These tests pin the doors shut from
inside pytest — and prove the muzzles are the pytest marker, not a missing
key, so live behaviour is untouched.
"""

import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3

from src import dhan_client as dc
from src import token_provider as tp
from src.validation import h4_shadow, trial


def test_the_market_data_door_returns_none_under_pytest_even_with_credentials():
    old = dc._client, dc._client_token
    dc._client, dc._client_token = None, None
    try:
        with mock.patch.dict(os.environ, {"DHAN_CLIENT_ID": "123"}), \
             mock.patch.object(tp, "get_token", return_value="a-live-looking-token"):
            assert dc._get_client() is None
            # the fail-safe wrappers degrade the same way they do with no creds
            assert dc.get_daily_ohlc("RELIANCE.NS", days=5) == []
            assert dc.get_live_price("RELIANCE.NS") is None
    finally:
        dc._client, dc._client_token = old


def test_the_escape_hatch_is_explicit_and_never_the_real_sdk():
    """Only ALPHA_ALLOW_SDK_CLIENT_IN_TESTS opens the door, and the one test
    that uses it also fakes the dhanhq module (see test_token_provider)."""
    old = dc._client, dc._client_token
    dc._client, dc._client_token = None, None
    built = []

    class _Fake:
        def DhanContext(self, cid, token):
            return (cid, token)

        def dhanhq(self, ctx):
            built.append(ctx)
            return object()
    try:
        with mock.patch.dict(sys.modules, {"dhanhq": _Fake()}), \
             mock.patch.dict(os.environ, {"DHAN_CLIENT_ID": "123",
                                          "ALPHA_ALLOW_SDK_CLIENT_IN_TESTS": "1"}), \
             mock.patch.object(tp, "get_token", return_value="t"):
            assert dc._get_client() is not None and built == [("123", "t")]
    finally:
        dc._client, dc._client_token = old


def test_h4_shadow_skips_when_bars_fn_is_the_live_default_even_with_a_conn():
    """sleep_phase passes a conn, so the older conn/record_fn muzzle never
    fired and Task J dialled Dhan through _default_bars_fn."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    trial.ensure_schema(conn)
    entries = [{"short_id": "sp1", "ticker": "NIFTY 50", "date": "2026-09-01",
                "decision": "approved", "outcome": None,
                "spread": {"strategy": "bear_put_spread", "expiry": "2026-09-30",
                           "legs": [], "lot_size": 65, "max_loss": 1.0, "max_profit": 1.0}}]
    with mock.patch.object(dc, "get_daily_ohlc", side_effect=AssertionError("dialled Dhan")):
        s = h4_shadow.run_shadow_pass(conn, entries=entries)
    assert s["skips"].get("muzzled_under_pytest")
    # an injected bars_fn still runs the pass
    s2 = h4_shadow.run_shadow_pass(conn, entries=entries, bars_fn=lambda t: [])
    assert "muzzled_under_pytest" not in s2["skips"]
