"""
Outgoing Discord webhook client (async).

Sends messages to a Discord channel via an incoming-webhook URL — the
lightweight push path, separate from the interactive bot in
src/discord_bot.py (which needs a bot token and a running gateway
connection). Used by the notifier and the API's background loops to push
watchlist alerts and resolved-trade "Episodes" to the phone.

Fully fail-safe by design: no DISCORD_WEBHOOK_URL configured, no httpx
installed, or any network error just prints a note and returns False —
callers never need a try/except and the engine is never blocked.

Setup: in Discord, channel settings -> Integrations -> Webhooks -> New
Webhook -> Copy URL, then add to .env:

    DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."

Optional `thread_id` (the numeric id of an existing thread in the same
channel) groups related messages — e.g. one thread per trade discussion.
"""

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_env()

# Discord hard-caps message content at 2000 characters.
DISCORD_MESSAGE_LIMIT = 2000
REQUEST_TIMEOUT_SECONDS = 10


def _webhook_url() -> str:
    """Read at call time (not import time) so tests can set/clear the env
    var freely. Strips optional surrounding quotes, matching how .env
    values are commonly written."""
    return (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip().strip('"').strip("'")


async def send_webhook_message(content: str, thread_id: str = None) -> bool:
    """POST one message to the configured webhook. Returns True on a 2xx
    response, False for everything else (unconfigured, bad response,
    network error) — never raises."""
    url = _webhook_url()
    if not url or not content:
        return False

    try:
        import httpx
    except ImportError as e:
        print(f"  (discord webhook skipped: httpx not installed: {e})")
        return False

    params = {"thread_id": str(thread_id)} if thread_id else None
    payload = {"content": content[:DISCORD_MESSAGE_LIMIT]}
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=payload, params=params)
        if resp.status_code >= 300:
            print(f"  (discord webhook failed: HTTP {resp.status_code})")
            return False
        return True
    except Exception as e:
        print(f"  (discord webhook failed to send: {e})")
        return False
