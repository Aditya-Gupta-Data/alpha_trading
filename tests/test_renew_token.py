"""
Tests for the DhanHQ token auto-renewal script (src/renew_token.py) --
the .env parsing/rewriting logic and the handling of Dhan's renewal JSON
keys. Pure functions only: no network call, and the real .env is never
read or written.

Run either of these from the project folder:
    python tests/test_renew_token.py       (simple, no extra installs)
    python -m pytest tests/                (if you have pytest)
"""

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.renew_token import (extract_new_token, read_credentials,
                             replace_token, token_expiry)


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
