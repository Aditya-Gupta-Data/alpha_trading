"""
Tests for the DhanHQ token auto-renewal script (src/renew_token.py) --
the .env parsing/rewriting logic, the handling of Dhan's renewal JSON
keys, and the V2 (PIN + TOTP) headless flow. Offline: every network call
is mocked, and the real .env is never read or written (temp files only).

Run either of these from the project folder:
    python tests/test_renew_token.py       (simple, no extra installs)
    python -m pytest tests/                (if you have pytest)
"""

import base64
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import renew_token as rt
from src.renew_token import (extract_new_token, generate_totp,
                             read_credentials, read_env_values,
                             replace_token, token_expiry, v2_ready)


SAMPLE_ENV = (
    "# alpha_trading secrets\n"
    "GEMINI_API_KEY=abc123\n"
    "\n"
    'DHAN_CLIENT_ID="1109738713"\n'
    "DHAN_ACCESS_TOKEN=  'eyJold.token.here'  \n"
)


def fake_jwt(exp: int) -> str:
    """Unsigned JWT-shaped string with just an exp claim, for expiry tests."""
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'HS256'})}.{part({'exp': exp})}.fakesig"


def test_read_credentials_strips_quotes_and_whitespace():
    client_id, token = read_credentials(SAMPLE_ENV)
    assert client_id == "1109738713"      # double quotes stripped
    assert token == "eyJold.token.here"   # single quotes + padding stripped


def test_read_credentials_tolerates_missing_keys():
    assert read_credentials("GEMINI_API_KEY=abc\n") == (None, None)
    assert read_credentials("") == (None, None)


def test_replace_token_swaps_only_the_token_line():
    updated = replace_token(SAMPLE_ENV, "eyJnew.token")
    assert "DHAN_ACCESS_TOKEN=eyJnew.token\n" in updated
    assert "eyJold.token.here" not in updated
    # every other line survives byte-for-byte:
    assert "# alpha_trading secrets\n" in updated
    assert "GEMINI_API_KEY=abc123\n" in updated
    assert 'DHAN_CLIENT_ID="1109738713"\n' in updated


def test_replace_token_appends_when_line_is_missing():
    updated = replace_token("GEMINI_API_KEY=abc123\n", "eyJnew.token")
    assert updated.endswith("DHAN_ACCESS_TOKEN=eyJnew.token\n")
    assert "GEMINI_API_KEY=abc123" in updated


def test_extract_new_token_handles_both_dhan_keys():
    assert extract_new_token({"token": "eyJfromtoken"}) == "eyJfromtoken"
    assert extract_new_token({"accessToken": "eyJfromaccess"}) == "eyJfromaccess"
    # documented key wins when both are present:
    assert extract_new_token({"token": "eyJa", "accessToken": "eyJb"}) == "eyJa"
    assert extract_new_token({"token": "  eyJpadded  "}) == "eyJpadded"


def test_extract_new_token_rejects_unusable_replies():
    assert extract_new_token({}) is None
    assert extract_new_token({"token": ""}) is None
    assert extract_new_token({"token": None}) is None
    assert extract_new_token({"errorMessage": "Invalid Token"}) is None
    assert extract_new_token("not a dict") is None
    assert extract_new_token(None) is None


def test_token_expiry_reads_the_jwt_exp_claim():
    assert token_expiry(fake_jwt(0)) == "1970-01-01T00:00:00+00:00"
    assert token_expiry(fake_jwt(1798761600)).startswith("2027-01-01")


def test_token_expiry_is_none_for_garbage():
    assert token_expiry("not-a-jwt") is None
    assert token_expiry("a.!!!notbase64!!!.c") is None
    assert token_expiry("") is None


# ------------------------------------------------------------ V2 flow

V2_ENV = (
    'DHAN_CLIENT_ID="1109738713"\n'
    "DHAN_ACCESS_TOKEN=eyJold.token.here\n"
    "DHAN_PIN=123456\n"
    "DHAN_TOTP_SECRET=JBSWY3DPEHPK3PXP\n"
    "DHAN_API_KEY=my-app-key\n"
    "DHAN_API_SECRET=my-app-secret\n"
)


def test_read_env_values_and_v2_ready():
    creds = read_env_values(V2_ENV)
    assert creds["DHAN_PIN"] == "123456"
    assert creds["DHAN_TOTP_SECRET"] == "JBSWY3DPEHPK3PXP"
    assert v2_ready(creds)
    # Missing any of the three required keys -> not ready (legacy path):
    assert not v2_ready(read_env_values(SAMPLE_ENV))
    assert not v2_ready(read_env_values(V2_ENV.replace("DHAN_PIN", "X_PIN")))


def test_generate_totp_produces_six_digits():
    code = generate_totp("JBSWY3DPEHPK3PXP")   # RFC test secret
    assert code is not None and len(code) == 6 and code.isdigit()
    # lowercase/spaced secrets are normalized rather than rejected:
    assert generate_totp("jbsw y3dp ehpk 3pxp") is not None


def test_generate_totp_failsafe_on_garbage_secret():
    assert generate_totp("!!!not-base32!!!") is None


def test_request_v2_token_sends_pin_totp_and_app_headers():
    captured = {}

    class FakeResponse:
        def read(self):
            return json.dumps({"accessToken": "eyJnew.v2.token",
                               "expiryTime": "2026-07-09T07:00:00"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["app_id"] = req.get_header("App_id")
        captured["app_secret"] = req.get_header("App_secret")
        return FakeResponse()

    with mock.patch.object(rt.urllib.request, "urlopen", fake_urlopen):
        body = rt.request_v2_token(read_env_values(V2_ENV), "654321")
    assert body["accessToken"] == "eyJnew.v2.token"
    assert captured["method"] == "POST"
    assert "dhanClientId=1109738713" in captured["url"]
    assert "pin=123456" in captured["url"] and "totp=654321" in captured["url"]
    assert captured["app_id"] == "my-app-key"
    assert captured["app_secret"] == "my-app-secret"


def test_renew_v2_end_to_end_rewrites_env(tmp_path=None):
    """Full V2 renewal against temp .env files — the real .env is never
    involved (module paths are patched for the duration)."""
    tmp = Path(tempfile.mkdtemp())
    env_file, bak_file = tmp / ".env", tmp / ".env.bak"
    env_file.write_text(V2_ENV)

    with mock.patch.object(rt, "ENV_PATH", env_file), \
         mock.patch.object(rt, "BACKUP_PATH", bak_file), \
         mock.patch.object(rt, "request_v2_token",
                           return_value={"accessToken": "eyJfresh.v2",
                                         "expiryTime": "2026-07-09T07:00:00"}):
        assert rt.renew() == 0

    updated = env_file.read_text()
    assert "DHAN_ACCESS_TOKEN=eyJfresh.v2\n" in updated
    assert "eyJold.token.here" not in updated
    assert "DHAN_PIN=123456" in updated            # other lines untouched
    assert bak_file.read_text() == V2_ENV          # backup preserved


def test_renew_dispatches_v2_when_creds_present_else_legacy():
    tmp = Path(tempfile.mkdtemp())
    env_file = tmp / ".env"

    env_file.write_text(V2_ENV)
    with mock.patch.object(rt, "ENV_PATH", env_file), \
         mock.patch.object(rt, "renew_v2", return_value=0) as v2, \
         mock.patch.object(rt, "renew_legacy", return_value=0) as legacy:
        rt.renew()
    assert v2.called and not legacy.called

    env_file.write_text(SAMPLE_ENV)  # no V2 keys -> legacy fallback
    with mock.patch.object(rt, "ENV_PATH", env_file), \
         mock.patch.object(rt, "fetch_gcp_secrets", return_value={}), \
         mock.patch.object(rt, "renew_v2", return_value=0) as v2, \
         mock.patch.object(rt, "renew_legacy", return_value=0) as legacy:
        rt.renew()
    assert legacy.called and not v2.called


# --- TOTP-rejection retry (ledger Issue 10, 2026-07-10) ---------------------
# With the single 07:00 IST cadence the token expires the moment its renewal
# is due — a transient "Invalid TOTP" must retry in the NEXT TOTP window
# instead of costing a whole market day.

TOTP_REJECTION = {"message": "Invalid TOTP", "status": "error"}


def _v2_tmp_paths():
    tmp = Path(tempfile.mkdtemp())
    return tmp / ".env", tmp / ".env.bak"


def test_renew_v2_retries_a_totp_rejection_in_the_next_window():
    env_file, bak_file = _v2_tmp_paths()
    env_file.write_text(V2_ENV)
    waits = []
    with mock.patch.object(rt, "ENV_PATH", env_file), \
         mock.patch.object(rt, "BACKUP_PATH", bak_file), \
         mock.patch.object(rt, "generate_totp",
                           side_effect=["111111", "222222"]), \
         mock.patch.object(rt, "request_v2_token",
                           side_effect=[TOTP_REJECTION,
                                        {"accessToken": "eyJsecond.try",
                                         "expiryTime": "2026-07-11T07:00:00"}]) as req:
        rc = rt.renew_v2(V2_ENV, wait_fn=waits.append)
    assert rc == 0
    assert waits == [rt._TOTP_RETRY_WAIT_SECONDS]     # waited exactly once
    assert req.call_count == 2
    # each attempt used a FRESH totp code, not a reuse of the rejected one
    assert req.call_args_list[0][0][1] == "111111"
    assert req.call_args_list[1][0][1] == "222222"
    assert "DHAN_ACCESS_TOKEN=eyJsecond.try\n" in env_file.read_text()


def test_renew_v2_gives_up_after_the_attempt_budget():
    env_file, bak_file = _v2_tmp_paths()
    env_file.write_text(V2_ENV)
    waits = []
    with mock.patch.object(rt, "ENV_PATH", env_file), \
         mock.patch.object(rt, "BACKUP_PATH", bak_file), \
         mock.patch.object(rt, "generate_totp", return_value="111111"), \
         mock.patch.object(rt, "request_v2_token",
                           return_value=TOTP_REJECTION) as req:
        rc = rt.renew_v2(V2_ENV, attempts=3, wait_fn=waits.append)
    assert rc == 1
    assert req.call_count == 3
    assert waits == [rt._TOTP_RETRY_WAIT_SECONDS] * 2  # no wait after the last try
    assert env_file.read_text() == V2_ENV              # .env untouched on failure
    assert not bak_file.exists()


def test_renew_v2_does_not_retry_non_totp_rejections():
    env_file, bak_file = _v2_tmp_paths()
    env_file.write_text(V2_ENV)
    waits = []
    with mock.patch.object(rt, "ENV_PATH", env_file), \
         mock.patch.object(rt, "BACKUP_PATH", bak_file), \
         mock.patch.object(rt, "generate_totp", return_value="111111"), \
         mock.patch.object(rt, "request_v2_token",
                           return_value={"message": "Invalid credentials",
                                         "status": "error"}) as req:
        rc = rt.renew_v2(V2_ENV, wait_fn=waits.append)
    assert rc == 1
    assert req.call_count == 1     # retrying a bad PIN would never succeed
    assert waits == []
    assert env_file.read_text() == V2_ENV


def test_renew_v2_does_not_retry_transport_failures():
    waits = []
    with mock.patch.object(rt, "generate_totp", return_value="111111"), \
         mock.patch.object(rt, "request_v2_token", return_value=None) as req:
        rc = rt.renew_v2(V2_ENV, wait_fn=waits.append)
    assert rc == 1
    assert req.call_count == 1 and waits == []


def test_is_totp_rejection_shapes():
    assert rt._is_totp_rejection(TOTP_REJECTION)
    assert rt._is_totp_rejection('{"message": "invalid totp"}')
    assert not rt._is_totp_rejection({"message": "Invalid credentials"})
    assert not rt._is_totp_rejection(None)


# --- GCP Secret Manager layer (decision #47: VM-side credentials) ----------

def _sm_payload(value: str) -> bytes:
    return json.dumps({"payload": {
        "data": base64.b64encode(value.encode()).decode()}}).encode()


def test_fetch_gcp_secrets_is_silent_off_gcp():
    # no metadata server (the Mac case) -> {} with no secret fetch attempted
    with mock.patch.object(rt, "_metadata_get",
                           side_effect=OSError("no metadata server")):
        assert rt.fetch_gcp_secrets(["DHAN_PIN", "DHAN_TOTP_SECRET"]) == {}


def test_fetch_gcp_secrets_decodes_only_mapped_keys():
    def fake_metadata(path, timeout=5):
        if "token" in path:
            return json.dumps({"access_token": "oauth-tok"})
        return "proj-id\n"

    class FakeResp:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    seen_urls = []

    def fake_urlopen(req, timeout=None, context=None):
        seen_urls.append(req.full_url)
        assert req.get_header("Authorization") == "Bearer oauth-tok"
        value = "pin-1234" if "dhan-pin" in req.full_url else "totp-abc"
        return FakeResp(_sm_payload(value))

    with mock.patch.object(rt, "_metadata_get", fake_metadata), \
         mock.patch.object(rt.urllib.request, "urlopen", fake_urlopen):
        out = rt.fetch_gcp_secrets(
            ["DHAN_PIN", "DHAN_TOTP_SECRET", "NOT_A_MAPPED_KEY"])
    assert out == {"DHAN_PIN": "pin-1234", "DHAN_TOTP_SECRET": "totp-abc"}
    assert all("projects/proj-id/secrets/dhan-" in u for u in seen_urls)
    assert len(seen_urls) == 2  # the unmapped key never hit the network


def test_fetch_gcp_secrets_partial_failure_degrades_per_key():
    def fake_metadata(path, timeout=5):
        return (json.dumps({"access_token": "t"}) if "token" in path
                else "proj-id")

    class FakeResp:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None, context=None):
        if "dhan-pin" in req.full_url:
            raise OSError("403 access denied")
        return FakeResp(_sm_payload("totp-abc"))

    with mock.patch.object(rt, "_metadata_get", fake_metadata), \
         mock.patch.object(rt.urllib.request, "urlopen", fake_urlopen):
        out = rt.fetch_gcp_secrets(["DHAN_PIN", "DHAN_TOTP_SECRET"])
    assert out == {"DHAN_TOTP_SECRET": "totp-abc"}  # the rest still arrive


def test_renew_never_consults_secret_manager_when_env_is_complete():
    tmp = Path(tempfile.mkdtemp())
    env_file = tmp / ".env"
    env_file.write_text(V2_ENV)
    with mock.patch.object(rt, "ENV_PATH", env_file), \
         mock.patch.object(rt, "fetch_gcp_secrets",
                           side_effect=AssertionError("SM consulted!")), \
         mock.patch.object(rt, "renew_v2", return_value=0) as v2:
        assert rt.renew() == 0
    assert v2.called  # .env keys always win — vault untouched


def test_renew_fills_missing_v2_keys_from_secret_manager():
    tmp = Path(tempfile.mkdtemp())
    env_file = tmp / ".env"
    env_file.write_text(SAMPLE_ENV)  # client id + token only, no V2 keys
    fetched = {"DHAN_PIN": "1234", "DHAN_TOTP_SECRET": "BASE32SECRET",
               "DHAN_API_KEY": "app-id", "DHAN_API_SECRET": "app-sec"}
    with mock.patch.object(rt, "ENV_PATH", env_file), \
         mock.patch.object(rt, "fetch_gcp_secrets", return_value=fetched), \
         mock.patch.object(rt, "renew_v2", return_value=0) as v2, \
         mock.patch.object(rt, "renew_legacy", return_value=0) as legacy:
        assert rt.renew() == 0
    assert v2.called and not legacy.called
    creds = v2.call_args.args[1]           # merged creds handed to the V2 flow
    assert creds["DHAN_PIN"] == "1234"
    assert creds["DHAN_CLIENT_ID"] == "1109738713"   # .env value retained


def test_renew_v2_fails_cleanly_without_touching_env():
    tmp = Path(tempfile.mkdtemp())
    env_file, bak_file = tmp / ".env", tmp / ".env.bak"
    env_file.write_text(V2_ENV)
    with mock.patch.object(rt, "ENV_PATH", env_file), \
         mock.patch.object(rt, "BACKUP_PATH", bak_file), \
         mock.patch.object(rt, "request_v2_token", return_value=None):
        assert rt.renew() == 1
    assert env_file.read_text() == V2_ENV   # untouched on failure
    assert not bak_file.exists()


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
