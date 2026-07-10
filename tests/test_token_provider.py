"""
Tests for src/token_provider.py — the self-healing DHAN_ACCESS_TOKEN
source (ledger Issue 5: an externally-renewed token must reach a running
session without a restart). Entirely offline: temp .env files only, the
real .env is never read or written.

Run from the project folder:
    python tests/test_token_provider.py      (simple, no extra installs)
    python -m pytest tests/                  (if you have pytest)
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import dhan_client as dc
from src import token_provider as tp


def _reset_provider():
    tp._cached_mtime_ns = None
    tp._cached_token = None
    tp._override = None


def _temp_env(text: str) -> Path:
    env = Path(tempfile.mkdtemp()) / ".env"
    env.write_text(text)
    return env


def test_get_token_reads_the_env_file():
    _reset_provider()
    env = _temp_env("DHAN_CLIENT_ID=123\nDHAN_ACCESS_TOKEN=eyJfirst\n")
    with mock.patch.object(tp, "ENV_PATH", env):
        assert tp.get_token() == "eyJfirst"


def test_get_token_sees_a_mid_session_renewal():
    """The Issue-5 scenario: the token line changes on disk after startup
    (exactly what renew_token._write_new_token does) — the next
    get_token() must return the NEW token, no restart, no refresh call."""
    _reset_provider()
    env = _temp_env("DHAN_ACCESS_TOKEN=eyJold\n")
    with mock.patch.object(tp, "ENV_PATH", env):
        assert tp.get_token() == "eyJold"
        env.write_text("DHAN_ACCESS_TOKEN=eyJrenewed\n")
        # force a distinct mtime even on coarse-granularity filesystems
        os.utime(env, ns=(env.stat().st_atime_ns, env.stat().st_mtime_ns + 1))
        assert tp.get_token() == "eyJrenewed"


def test_get_token_strips_whitespace_and_quotes():
    _reset_provider()
    env = _temp_env('DHAN_ACCESS_TOKEN = "eyJquoted"  \n')
    with mock.patch.object(tp, "ENV_PATH", env):
        assert tp.get_token() == "eyJquoted"


def test_override_wins_until_cleared():
    _reset_provider()
    env = _temp_env("DHAN_ACCESS_TOKEN=eyJfile\n")
    with mock.patch.object(tp, "ENV_PATH", env):
        tp.set_override("eyJpushed")
        assert tp.get_token() == "eyJpushed"
        tp.clear_override()
        assert tp.get_token() == "eyJfile"


def test_missing_env_file_falls_back_to_process_environ():
    """Fail-safe: no .env must never kill a running session — degrade to
    whatever the process loaded at startup."""
    _reset_provider()
    missing = Path(tempfile.mkdtemp()) / "does-not-exist.env"
    with mock.patch.object(tp, "ENV_PATH", missing), \
         mock.patch.dict(os.environ, {"DHAN_ACCESS_TOKEN": "eyJstartup"}):
        assert tp.get_token() == "eyJstartup"


def test_env_without_token_line_falls_back_to_environ():
    _reset_provider()
    env = _temp_env("DHAN_CLIENT_ID=123\n# no token line\n")
    with mock.patch.object(tp, "ENV_PATH", env), \
         mock.patch.dict(os.environ, {"DHAN_ACCESS_TOKEN": "eyJenviron"}):
        assert tp.get_token() == "eyJenviron"


def test_refresh_forces_a_reread_on_an_unchanged_mtime():
    """A same-nanosecond rewrite (theoretical on coarse filesystems) is
    invisible to the stat check — refresh() must still pick it up."""
    _reset_provider()
    env = _temp_env("DHAN_ACCESS_TOKEN=eyJv1\n")
    with mock.patch.object(tp, "ENV_PATH", env):
        assert tp.get_token() == "eyJv1"
        stat = env.stat()
        env.write_text("DHAN_ACCESS_TOKEN=eyJv2\n")
        os.utime(env, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # freeze mtime
        assert tp.refresh() == "eyJv2"


# --- the dhan_client seam: one client rebuild per token change -------------

class _FakeDhanhqModule:
    """Stand-in for the dhanhq package: records every client build and
    which token it was built with."""

    def __init__(self):
        self.built_with = []

    def DhanContext(self, cid, token):
        return (cid, token)

    def dhanhq(self, ctx):
        self.built_with.append(ctx[1])
        return object()   # each build is a distinct client identity


def test_dhan_client_rebuilds_exactly_when_the_token_changes():
    fake = _FakeDhanhqModule()
    old_client, old_token = dc._client, dc._client_token
    dc._client, dc._client_token = None, None
    try:
        with mock.patch.dict(sys.modules, {"dhanhq": fake}), \
             mock.patch.dict(os.environ, {"DHAN_CLIENT_ID": "123"}), \
             mock.patch.object(tp, "get_token",
                               side_effect=["tokA", "tokA", "tokB"]):
            c1 = dc._get_client()
            c2 = dc._get_client()   # same token -> cached client reused
            c3 = dc._get_client()   # token changed -> rebuilt
        assert c1 is c2
        assert c3 is not c1
        assert fake.built_with == ["tokA", "tokB"]
    finally:
        dc._client, dc._client_token = old_client, old_token


def test_dhan_client_returns_none_without_a_token():
    old_client, old_token = dc._client, dc._client_token
    dc._client, dc._client_token = None, None
    try:
        with mock.patch.dict(os.environ, {"DHAN_CLIENT_ID": "123"}), \
             mock.patch.object(tp, "get_token", return_value=None):
            assert dc._get_client() is None
    finally:
        dc._client, dc._client_token = old_client, old_token


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError:
            print(f"FAIL  {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed.")
