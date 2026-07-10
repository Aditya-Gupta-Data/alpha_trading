"""
src/token_provider.py — self-healing DHAN_ACCESS_TOKEN source
=============================================================

Why this module exists (observation-week Issue 5, 2026-07-09): the live
trading session read DHAN_ACCESS_TOKEN into os.environ ONCE at startup
(via each module's _load_env + os.environ.setdefault) and never looked at
.env again. Dhan allows exactly one active token per client id (decision
#48), so the moment any external renewal minted a new token, the running
session's in-memory copy went dead — DH-906 on every call until a human
restarted the process.

This module makes the token a LIVE value instead of a startup constant:

    from src import token_provider
    token = token_provider.get_token()

  * get_token() stats .env on every call (a stat is ~microseconds — free
    at the engine's 60s/900s cadences). When the file's mtime changes —
    which is exactly what renew_token._write_new_token causes — the token
    is re-read from disk mid-session, no restart needed.
  * set_override(token) accepts an in-memory refresh (tests, or a future
    push-style renewal) that wins over the file until clear_override().
  * Fail-safe: a missing/unreadable .env degrades to os.environ (which
    still holds the startup value), never raises, so the engine keeps
    running on the token it already had.

dhan_client._get_client() consults this provider per call and rebuilds
its SDK client whenever the token it was built with is no longer the
current one — that one seam heals every consumer (master_scheduler,
market_loop, live_bridge, plan_tracker, eod_summary) at once.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

_ENV_KEY = "DHAN_ACCESS_TOKEN"

# Module-level cache: (mtime_ns of .env when last parsed, token found then).
_cached_mtime_ns = None
_cached_token = None
_override = None


def _parse_env_token(env_text: str) -> str | None:
    """DHAN_ACCESS_TOKEN's value from raw .env text, or None. Mirrors the
    project's other .env readers: skip blanks/comments, split on the
    first '=', strip whitespace and one layer of quotes."""
    for line in env_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == _ENV_KEY:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1].strip()
            return value or None
    return None


def get_token() -> str | None:
    """The CURRENT access token: the in-memory override if set, else the
    freshest value in .env (re-read whenever the file changes), else the
    process environment as the last resort. Never raises."""
    global _cached_mtime_ns, _cached_token
    if _override:
        return _override
    try:
        mtime_ns = ENV_PATH.stat().st_mtime_ns
    except OSError:
        # .env missing/unreadable — keep running on whatever the process
        # already loaded at startup rather than dying mid-session.
        return _cached_token or os.environ.get(_ENV_KEY)
    if mtime_ns != _cached_mtime_ns:
        try:
            _cached_token = _parse_env_token(ENV_PATH.read_text())
            _cached_mtime_ns = mtime_ns
        except OSError:
            return _cached_token or os.environ.get(_ENV_KEY)
    return _cached_token or os.environ.get(_ENV_KEY)


def set_override(token: str) -> None:
    """Accept an in-memory token refresh that wins over .env until
    clear_override() — the push-style seam (and the test seam)."""
    global _override
    _override = (token or "").strip() or None


def clear_override() -> None:
    global _override
    _override = None


def refresh() -> str | None:
    """Force the next get_token() to re-read .env even if the mtime looks
    unchanged (e.g. a same-second rewrite on a coarse filesystem)."""
    global _cached_mtime_ns
    _cached_mtime_ns = None
    return get_token()
