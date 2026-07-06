"""
Alpha Trading -- DhanHQ access-token auto-renewal
==================================================

The DhanHQ Data API token (`DHAN_ACCESS_TOKEN` in .env) expires roughly
every 24 hours, and refreshing it has been a manual dashboard chore since
the DhanHQ migration. This standalone script automates it:

  1. reads DHAN_CLIENT_ID + DHAN_ACCESS_TOKEN straight from the repo's
     .env (stripping any surrounding quotes/whitespace),
  2. calls GET https://api.dhan.co/v2/RenewToken with the `access-token`
     and `dhanClientId` headers,
  3. on success, rewrites ONLY the DHAN_ACCESS_TOKEN line in .env with
     the fresh token (a .env.bak copy of the old file is written first)
     and prints the new expiry timestamp.

If Dhan answers HTTP 400/401 or "Invalid Token", the current token is
already dead/corrupted and can't renew itself -- the script prints a
CRITICAL message telling you to paste a fresh token from the Dhan
dashboard (remember the base64 trick from HANDOVER.md when doing that on
the VM). .env is NEVER touched unless a plausible new token came back.

Like the other entry points, .env is resolved at the repo root, so the
same script works on the Mac (project folder) and the VM
(~/alpha_trading/.env). Run it manually, or schedule it (e.g. VM cron
before market hours):

    python3 -m src.renew_token

Exit code 0 on success, 1 on any failure -- cron/systemd friendly.
"""

import base64
import json
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import certifi

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
BACKUP_PATH = ROOT / ".env.bak"

RENEW_URL = "https://api.dhan.co/v2/RenewToken"
HTTP_TIMEOUT = 20  # seconds

CRITICAL_MSG = ("CRITICAL: Current token invalid. Please manually update .env "
                "with a fresh token from the Dhan dashboard.")


def _strip(value: str) -> str:
    """Trim whitespace and one layer of surrounding quotes."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1].strip()
    return value


def read_credentials(env_text: str) -> tuple:
    """(client_id, access_token) parsed from .env text -- either may be
    None if its line is missing."""
    values = {}
    for line in env_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = _strip(value)
    return values.get("DHAN_CLIENT_ID"), values.get("DHAN_ACCESS_TOKEN")


def replace_token(env_text: str, new_token: str) -> str:
    """The .env text with ONLY the DHAN_ACCESS_TOKEN line swapped for the
    new token (unquoted -- matching how the project's _load_env readers
    expect it); every other line is preserved byte-for-byte. Appends the
    line if it was somehow missing."""
    lines, replaced = [], False
    for line in env_text.splitlines():
        if not replaced and line.split("=", 1)[0].strip() == "DHAN_ACCESS_TOKEN":
            lines.append(f"DHAN_ACCESS_TOKEN={new_token}")
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        lines.append(f"DHAN_ACCESS_TOKEN={new_token}")
    return "\n".join(lines) + "\n"


def extract_new_token(payload) -> str:
    """The renewed token from Dhan's JSON reply -- documented as `token`,
    but some API versions use `accessToken`; handle both. None if neither
    key holds a usable string."""
    if not isinstance(payload, dict):
        return None
    for key in ("token", "accessToken"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def token_expiry(token: str) -> str:
    """ISO-8601 expiry read from the JWT's `exp` claim (decoded without
    verification -- we only want the timestamp). None if undecodable."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        return datetime.fromtimestamp(int(claims["exp"]),
                                      tz=timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return None


def renew() -> int:
    """Full renewal flow. Returns a process exit code (0 ok, 1 failed)."""
    if not ENV_PATH.exists():
        print(f"Token renewal failed: {ENV_PATH} does not exist.")
        return 1
    env_text = ENV_PATH.read_text()
    client_id, token = read_credentials(env_text)
    if not client_id or not token:
        print("Token renewal failed: DHAN_CLIENT_ID and/or DHAN_ACCESS_TOKEN "
              "missing from .env.")
        return 1

    req = urllib.request.Request(
        RENEW_URL,
        headers={"access-token": token, "dhanClientId": client_id},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_SSL_CTX) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        if e.code in (400, 401) or "invalid token" in detail.lower():
            print(CRITICAL_MSG)
        else:
            print("Token renewal failed: Dhan answered "
                  f"HTTP {e.code} — .env left untouched.")
        print(f"  (HTTP {e.code}: {detail[:300]})")
        return 1
    except Exception as e:
        print(f"Token renewal failed: {e} — .env left untouched.")
        return 1

    new_token = extract_new_token(body)
    if new_token is None:
        body_text = json.dumps(body) if not isinstance(body, str) else body
        if "invalid" in body_text.lower():
            print(CRITICAL_MSG)
        else:
            print("Token renewal failed: no token in Dhan's reply "
                  "— .env left untouched.")
        print(f"  (response: {body_text[:300]})")
        return 1

    BACKUP_PATH.write_text(env_text)  # old .env preserved as .env.bak
    ENV_PATH.write_text(replace_token(env_text, new_token))
    expiry = body.get("expiryTime") or token_expiry(new_token) or "unknown"
    print(f"Token renewed successfully. New expiry: {expiry}")
    print(f"  (.env updated; previous version saved to {BACKUP_PATH.name})")
    return 0


if __name__ == "__main__":
    sys.exit(renew())
