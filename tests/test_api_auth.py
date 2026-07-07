"""
Tests for optional API-key auth on src/api.py (Cloudflare / public exposure).

Offline — uses FastAPI TestClient only, no Dhan or Gemini calls on the
paths we hit.

Run:
    python tests/test_api_auth.py
    pytest tests/test_api_auth.py -v
"""

import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from src.api import app, _extract_api_key, _keys_match, _read_api_key
from starlette.requests import Request


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
    test_read_api_key_strips_quotes()
    test_read_api_key_none_when_unset()
    test_keys_match_rejects_wrong_length()
    test_keys_match_accepts_equal_key()
    test_extract_api_key_from_headers()
    test_open_when_api_key_unset()
    test_health_always_public()
    test_blocks_without_key_when_configured()
    test_blocks_wrong_key()
    test_accepts_x_api_key_header()
    test_accepts_bearer_authorization()
    test_options_preflight_without_key()
    print("All API auth tests passed.")
