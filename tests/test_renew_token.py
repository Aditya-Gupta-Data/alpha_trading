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
         mock.patch.object(rt, "renew_v2", return_value=0) as v2, \
         mock.patch.object(rt, "renew_legacy", return_value=0) as legacy:
        rt.renew()
    assert legacy.called and not v2.called


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
