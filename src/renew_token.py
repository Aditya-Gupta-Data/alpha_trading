"""
Alpha Trading -- DhanHQ access-token auto-renewal
==================================================

The DhanHQ Data API token (`DHAN_ACCESS_TOKEN` in .env) expires roughly
every 24 hours. This standalone script keeps it alive, V2-FIRST:

  V2 (preferred, post 2025-10 auth overhaul): when .env carries
     DHAN_CLIENT_ID + DHAN_PIN + DHAN_TOTP_SECRET (optionally
     DHAN_API_KEY / DHAN_API_SECRET app headers), it computes the current
     TOTP code via pyotp and POSTs auth.dhan.co/app/generateAccessToken —
     minting a BRAND-NEW 24h token headlessly. Works even when the old
     token is already dead: no manual dashboard trips, ever.

  Legacy (fallback only): without the V2 keys it calls the old
     api.dhan.co/v2/RenewToken — DEPRECATED by Dhan; answers DH-905 for
     tokens generated after the overhaul, and can only renew a token
     that's still alive. Kept so an un-migrated .env degrades loudly
     instead of breaking silently.

Either way, on success it rewrites ONLY the DHAN_ACCESS_TOKEN line in
.env (a .env.bak copy of the old file is written first) and prints the
new expiry. .env is NEVER touched unless a plausible new token came back.

V2 setup (one-time, on Dhan web): My Profile -> Access DhanHQ APIs ->
generate the API key + secret; enable TOTP 2FA on the account (profile ->
security) and copy the base32 authenticator secret -> put all of it plus
your login PIN into .env (see .env.example).

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


# ------------------------------------------------- V2 auth (2026-07-08)
#
# DhanHQ overhauled authentication effective 2025-10-01: the legacy
# /v2/RenewToken call above now answers DH-905 ("Renewal of token not
# allowed for this token type") for freshly dashboard-generated tokens.
# The replacement is the fully headless generateAccessToken flow — Dhan
# client id + login PIN + a TOTP code (from the authenticator secret you
# get when enabling TOTP 2FA on Dhan web) mint a brand-new 24h token every
# run, no still-valid old token required, no browser step.

V2_TOKEN_URL = "https://auth.dhan.co/app/generateAccessToken"

V2_ENV_KEYS = ("DHAN_CLIENT_ID", "DHAN_PIN", "DHAN_TOTP_SECRET",
               "DHAN_API_KEY", "DHAN_API_SECRET")


def read_env_values(env_text: str, keys=V2_ENV_KEYS) -> dict:
    """{key: stripped-value-or-None} for every requested .env key."""
    values = {}
    for line in env_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = _strip(value)
    return {k: (values.get(k) or None) for k in keys}


def v2_ready(creds: dict) -> bool:
    """True when the headless V2 flow can run: client id + PIN + TOTP
    secret. (API key/secret headers ride along when present.)"""
    return bool(creds.get("DHAN_CLIENT_ID") and creds.get("DHAN_PIN")
                and creds.get("DHAN_TOTP_SECRET"))


# --- GCP Secret Manager: the VM-side credential source (decision #47) -----
# The V2 keys (PIN / TOTP secret / API key+secret) are account-control
# credentials and never sit in the VM's .env. On a GCP VM they live in
# Secret Manager, granted per-secret to the instance's service account,
# and are fetched here at renewal time via the metadata-server OAuth
# token — pure stdlib, no google-cloud SDK dependency. On any non-GCP
# machine (the Mac) the metadata server doesn't exist and this whole
# layer silently no-ops, so .env keys keep working exactly as before.

GCP_METADATA_BASE = "http://metadata.google.internal/computeMetadata/v1"
SECRET_NAME_MAP = {
    "DHAN_PIN": "dhan-pin",
    "DHAN_TOTP_SECRET": "dhan-totp-secret",
    "DHAN_API_KEY": "dhan-api-key",
    "DHAN_API_SECRET": "dhan-api-secret",
}


def _metadata_get(path: str, timeout: float = 5) -> str:
    req = urllib.request.Request(f"{GCP_METADATA_BASE}/{path}",
                                 headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode()


def fetch_gcp_secrets(keys) -> dict:
    """{env_key: value} for the requested V2 keys, from Secret Manager.
    Fail-safe by contract: not on GCP / API disabled / access denied /
    a missing secret — each degrades to that key simply being absent,
    and completely off-GCP returns {} with no noise at all."""
    try:
        token = json.loads(_metadata_get(
            "instance/service-accounts/default/token"))["access_token"]
        project = _metadata_get("project/project-id").strip()
    except Exception:
        return {}  # not a GCP VM (e.g. the Mac) — silently skip
    fetched = {}
    for env_key in keys:
        secret = SECRET_NAME_MAP.get(env_key)
        if secret is None:
            continue
        url = (f"https://secretmanager.googleapis.com/v1/projects/{project}"
               f"/secrets/{secret}/versions/latest:access")
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT,
                                        context=_SSL_CTX) as resp:
                payload = json.loads(resp.read())
            fetched[env_key] = base64.b64decode(
                payload["payload"]["data"]).decode().strip()
        except Exception as e:
            print(f"  (Secret Manager: {secret} unavailable: {e})")
    return fetched


def generate_totp(secret: str) -> str | None:
    """The current 6-digit TOTP code for `secret`, or None (pyotp missing
    or an unusable secret — both reported by the caller, never raised)."""
    try:
        import pyotp
        return pyotp.TOTP(secret.replace(" ", "").upper()).now()
    except ImportError:
        print("Token renewal failed: pyotp is not installed — run "
              "`python3 -m pip install pyotp` (it's in requirements.txt).")
        return None
    except Exception as e:
        print(f"Token renewal failed: could not compute TOTP ({e}) — check "
              "DHAN_TOTP_SECRET in .env (the base32 authenticator secret).")
        return None


def request_v2_token(creds: dict, totp_code: str) -> dict | None:
    """One generateAccessToken call -> parsed JSON body, or None on any
    transport/HTTP failure (already printed). Never raises."""
    from urllib.parse import urlencode
    params = urlencode({"dhanClientId": creds["DHAN_CLIENT_ID"],
                        "pin": creds["DHAN_PIN"], "totp": totp_code})
    headers = {"Accept": "application/json"}
    # The API key/secret pair (12-month validity, from the "Access DhanHQ
    # APIs" dashboard page) authenticates the *app*; send when configured.
    if creds.get("DHAN_API_KEY"):
        headers["app_id"] = creds["DHAN_API_KEY"]
    if creds.get("DHAN_API_SECRET"):
        headers["app_secret"] = creds["DHAN_API_SECRET"]
    req = urllib.request.Request(f"{V2_TOKEN_URL}?{params}",
                                 headers=headers, method="POST", data=b"")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_SSL_CTX) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        print(f"Token renewal failed: Dhan V2 auth answered HTTP {e.code} "
              "— .env left untouched.")
        print(f"  (HTTP {e.code}: {detail[:300]})")
        print("  Check DHAN_PIN / DHAN_TOTP_SECRET / DHAN_API_KEY+SECRET in "
              ".env (see HANDOVER.md -> credentials).")
        return None
    except Exception as e:
        print(f"Token renewal failed: {e} — .env left untouched.")
        return None


def _write_new_token(env_text: str, new_token: str, expiry) -> None:
    BACKUP_PATH.write_text(env_text)  # old .env preserved as .env.bak
    ENV_PATH.write_text(replace_token(env_text, new_token))
    expiry = expiry or token_expiry(new_token) or "unknown"
    print(f"Token renewed successfully. New expiry: {expiry}")
    print(f"  (.env updated; previous version saved to {BACKUP_PATH.name})")


def renew_v2(env_text: str, creds: dict = None) -> int:
    """The headless V2 flow: PIN + TOTP -> a brand-new 24h access token.
    Unlike the legacy renewal, this works even when the old token is
    already dead — there is no manual-dashboard fallback ever needed as
    long as the V2 credentials (in .env, or Secret-Manager-fetched by
    the caller) stay valid."""
    if creds is None:
        creds = read_env_values(env_text)
    totp_code = generate_totp(creds["DHAN_TOTP_SECRET"])
    if totp_code is None:
        return 1
    body = request_v2_token(creds, totp_code)
    if body is None:
        return 1
    new_token = extract_new_token(body)
    if new_token is None:
        body_text = json.dumps(body) if not isinstance(body, str) else body
        print("Token renewal failed: no token in Dhan's V2 reply — .env "
              "left untouched.")
        print(f"  (response: {body_text[:300]})")
        return 1
    _write_new_token(env_text, new_token, body.get("expiryTime"))
    return 0


def renew_legacy(env_text: str) -> int:
    """The pre-2025-10 /v2/RenewToken flow — kept only as a fallback for
    .env files that don't carry the V2 credentials yet. Expect DH-905 for
    tokens generated after Dhan's auth overhaul."""
    client_id, token = read_credentials(env_text)
    if not client_id or not token:
        print("Token renewal failed: DHAN_CLIENT_ID and/or DHAN_ACCESS_TOKEN "
              "missing from .env.")
        return 1
    print("note: using the DEPRECATED legacy renewal (no DHAN_PIN + "
          "DHAN_TOTP_SECRET in .env) — set them up to stop the daily "
          "manual-paste cycle (HANDOVER.md -> credentials).")

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

    _write_new_token(env_text, new_token, body.get("expiryTime"))
    return 0


def renew() -> int:
    """Full renewal flow (V2-first). Returns a process exit code (0 ok,
    1 failed) — cron/systemd friendly, same contract as always."""
    if not ENV_PATH.exists():
        print(f"Token renewal failed: {ENV_PATH} does not exist.")
        return 1
    env_text = ENV_PATH.read_text()
    creds = read_env_values(env_text)
    if not v2_ready(creds):
        # VM path (decision #47): fill only the MISSING keys from GCP
        # Secret Manager — .env values always win when present.
        fetched = fetch_gcp_secrets(
            [k for k in V2_ENV_KEYS if not creds.get(k)])
        if fetched:
            print("  (V2 credentials from GCP Secret Manager: "
                  f"{', '.join(sorted(fetched))})")
            creds.update(fetched)
    if v2_ready(creds):
        return renew_v2(env_text, creds)
    return renew_legacy(env_text)


if __name__ == "__main__":
    sys.exit(renew())
