"""
Sends alerts.

Always prints to the screen. Also emails you via Gmail if credentials are
set in a local .env file (see .env.example and README.md for setup).
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
