"""
Sends alerts.

Always prints to the screen. Also emails you via Gmail if credentials are
set in a local .env file (see .env.example and README.md for setup).

Discord: send_discord_message() (async) pushes the same kind of message to
a Discord channel via src/discord_client.py when DISCORD_WEBHOOK_URL is set
in .env — used by the API's background loops for watchlist alerts and
resolved-trade Episodes. Fail-safe like email: unconfigured or failing
Discord never raises, it just returns False.
"""

import os
import smtplib
from email.message import EmailMessage
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

EMAIL_FROM = os.environ.get("ALERT_EMAIL_FROM")
EMAIL_APP_PASSWORD = os.environ.get("ALERT_EMAIL_APP_PASSWORD")
EMAIL_TO = os.environ.get("ALERT_EMAIL_TO") or EMAIL_FROM


def _send_email(subject: str, body: str) -> None:
    if not EMAIL_FROM or not EMAIL_APP_PASSWORD:
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.set_content(body)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_FROM, EMAIL_APP_PASSWORD)
            server.send_message(msg)
    except Exception as e:
        print(f"  (email failed to send: {e})")


def send_digest(subject: str, lines: list) -> None:
    _send_email(subject, "\n".join(lines))


async def send_discord_message(message: str, thread_id: str = None) -> bool:
    """Push one message to Discord via the webhook client. Async because
    the network call is httpx-async (the API's event loop awaits it
    directly). Returns False instead of raising when Discord is
    unconfigured or unreachable."""
    try:
        from src.discord_client import send_webhook_message
    except Exception as e:
        print(f"  (discord client unavailable: {e})")
        return False
    return await send_webhook_message(message, thread_id=thread_id)


def format_episode(episode: dict) -> str:
    """One resolved-trade Episode dict (from brain_map.build_episode_snapshot)
    -> the structured Discord message body."""
    sentiment = episode.get("market_sentiment") or {}
    lines = [
        f"📕 **Trade Episode — {episode.get('ticker')}**",
        f"Resolution: {episode.get('resolution')} | Verdict: {episode.get('verdict')}",
        (f"Entry {episode.get('entry_date')} @ Rs.{episode.get('entry_price')} → "
         f"Exit {episode.get('exit_date')} @ Rs.{episode.get('exit_price')}"),
        f"R-multiple: {episode.get('r_multiple')} | Net P&L: Rs.{episode.get('pnl_rs')}",
        f"Signal at entry: {episode.get('signal')}",
    ]
    if episode.get("pattern_tags"):
        lines.append("Pattern tags: " + ", ".join(episode["pattern_tags"]))
    if sentiment.get("score") is not None:
        lines.append(f"Market sentiment: {sentiment['score']:+.2f} "
                     f"({sentiment.get('headline_focus') or 'no focus'})")
    return "\n".join(lines)
