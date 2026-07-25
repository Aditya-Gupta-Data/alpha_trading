"""
Tests for optional API-key auth on src/api.py (Cloudflare / public exposure).

Offline and HERMETIC — the `hermetic_quotes` autouse fixture below stubs the
watchlist store and the quote fetcher, so nothing here touches the network or
production data.

Why the fixture exists (2026-07-25, Phase-3 test streamlining): the three
tests that PASS auth actually execute the /api/watchlist handler, which calls
_get_quotes() -> get_quote() once per distinct ticker in the REAL watchlist
(84 of them). Each test therefore fired 84 live quote requests and took
200-257s — 11 of the suite's 14 minutes, in three tests. Worse, they read
live production data and would hang or fail on a network-less CI box: the
same bug class as the 07-22 journal-drift and 07-23 digest-queue leaks. The
handler logic still runs for real here; only the data door is stubbed.

Run:
    python tests/test_api_auth.py
    pytest tests/test_api_auth.py -v
"""

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from src import api as _api
from src.api import app, _extract_api_key, _keys_match, _read_api_key
from starlette.requests import Request

_FAKE_ITEMS = [
    {"ticker": "RELIANCE.NS", "type": "stock",
     "condition": "percent_up", "value": 3.0},
    {"ticker": "TCS.NS", "type": "stock"},
]
_FAKE_QUOTE = {"ticker": "RELIANCE.NS", "current_price": 2500.0,
               "prev_close": 2480.0, "percent_change": 0.81}


@pytest.fixture(autouse=True)
def hermetic_quotes():
    """No network, no production watchlist, no cross-test cache bleed."""
    _api._quote_cache, _api._cache_at = {}, None
    with mock.patch("src.web.watchlist_store.load_items",
                    return_value=list(_FAKE_ITEMS)), \
         mock.patch("src.api.get_quote", return_value=dict(_FAKE_QUOTE)):
        yield
    _api._quote_cache, _api._cache_at = {}, None


def _client() -> TestClient:
    return TestClient(app)


def test_read_api_key_strips_quotes():
    with mock.patch.dict(os.environ, {"API_KEY": '  "my-secret"  '}, clear=False):
        assert _read_api_key() == "my-secret"


def test_read_api_key_none_when_unset():
    env = os.environ.copy()
    env.pop("API_KEY", None)
    with mock.patch.dict(os.environ, env, clear=True):
        assert _read_api_key() is None


def test_keys_match_rejects_wrong_length():
    assert not _keys_match("short", "much-longer-secret")


def test_keys_match_accepts_equal_key():
    assert _keys_match("alpha-trading-key", "alpha-trading-key")
    assert not _keys_match("alpha-trading-key", "alpha-trading-kez")


def test_extract_api_key_from_headers():
    scope = {"type": "http", "headers": [(b"x-api-key", b"abc123")],
             "method": "GET", "path": "/", "query_string": b""}
    req = Request(scope)
    assert _extract_api_key(req) == "abc123"

    scope["headers"] = [(b"authorization", b"Bearer tok.en")]
    req = Request(scope)
    assert _extract_api_key(req) == "tok.en"


def test_open_when_api_key_unset():
    env = {k: v for k, v in os.environ.items() if k != "API_KEY"}
    with mock.patch.dict(os.environ, env, clear=True):
        r = _client().get("/api/watchlist")
        assert r.status_code != 401


def test_health_always_public():
    with mock.patch.dict(os.environ, {"API_KEY": "gate-secret"}, clear=False):
        r = _client().get("/api/health")
        assert r.status_code == 200
        assert r.json()["auth"] == "required"


def test_blocks_without_key_when_configured():
    with mock.patch.dict(os.environ, {"API_KEY": "gate-secret"}, clear=False):
        r = _client().get("/api/watchlist")
        assert r.status_code == 401
        assert r.json()["ok"] is False


def test_blocks_wrong_key():
    with mock.patch.dict(os.environ, {"API_KEY": "gate-secret"}, clear=False):
        r = _client().get("/api/watchlist", headers={"X-API-Key": "wrong-key"})
        assert r.status_code == 401


def test_accepts_x_api_key_header():
    with mock.patch.dict(os.environ, {"API_KEY": "gate-secret"}, clear=False):
        r = _client().get("/api/watchlist", headers={"X-API-Key": "gate-secret"})
        assert r.status_code != 401


def test_accepts_bearer_authorization():
    with mock.patch.dict(os.environ, {"API_KEY": "gate-secret"}, clear=False):
        r = _client().get(
            "/api/watchlist",
            headers={"Authorization": "Bearer gate-secret"},
        )
        assert r.status_code != 401


def test_options_preflight_without_key():
    with mock.patch.dict(os.environ, {"API_KEY": "gate-secret"}, clear=False):
        r = _client().options("/api/watchlist")
        assert r.status_code != 401


if __name__ == "__main__":
    # Delegate to pytest: calling the test fns directly would bypass the
    # autouse hermetic_quotes fixture and hit the live quote API 84 times
    # per watchlist-touching test (the exact 2026-07-25 bug).
    raise SystemExit(pytest.main([__file__, "-v"]))
