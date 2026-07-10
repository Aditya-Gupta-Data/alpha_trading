"""
Tests for src/dhan_guard.py — the response-shape hardening & audit
wrapper. Entirely offline: the SDK client is a Mock, both the flat and
the malformed doubly-nested DH-906 failure shapes are simulated here.

Run from the project folder:
    python tests/test_dhan_guard.py          (simple, no extra installs)
    python -m pytest tests/                  (if you have pytest)
"""

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import dhan_client as dc
from src.dhan_guard import (DhanApiError, SafeDhanClient, classify_failure,
                            unwrap_payload)

# The exact failure shape observed live (master_scheduler.log, 2026-07-09):
DH906_RESPONSE = {"status": "failure",
                  "remarks": {"error_code": "DH-906",
                              "error_type": "Order_Error",
                              "error_message": "Invalid Token"},
                  "data": ""}

DATES = ["2026-07-14", "2026-07-21", "2026-07-28"]
CHAIN_INNER = {"last_price": 24065.2, "oc": {"24000.000000": {
    "ce": {"last_price": 120.5}, "pe": {"last_price": 95.0}}}}
QUOTE_SEC = {"last_price": 3145.0, "ohlc": {"close": 3120.0}}
INSTR = {"id": "13", "seg": "IDX_I", "inst": "INDEX"}


def _patched(instr=INSTR):
    """(resolve_patch, client_patch, rate_pause_patch) — no real network,
    and the failure-retry pause is zeroed so tests stay fast."""
    return (mock.patch.object(dc, "_resolve", return_value=instr),
            mock.patch.object(dc, "_get_client", return_value=mock.Mock()),
            mock.patch.object(dc, "_RATE_PAUSE", 0))


# --------------------------------------------------------- classify_failure

def test_classify_failure_passes_success_through():
    assert classify_failure({"status": "success", "data": DATES}) is None


def test_classify_failure_reads_the_nested_dh906_remarks():
    err = classify_failure(DH906_RESPONSE)
    assert err is not None
    assert err.code == "DH-906"
    assert err.message == "Invalid Token"
    assert err.is_auth_error


def test_classify_failure_reads_raw_error_bodies_without_status():
    err = classify_failure({"errorType": "Invalid_Authentication",
                            "errorCode": "DH-901",
                            "errorMessage": "token is invalid or expired"})
    assert err.code == "DH-901"
    assert err.is_auth_error


def test_classify_failure_reads_string_remarks():
    err = classify_failure({"status": "failure",
                            "remarks": "DH-906 : Invalid Token"})
    assert err.code == "DH-906"


def test_classify_failure_handles_non_dict_and_unknown_shapes():
    assert classify_failure("gateway timeout").code == "BAD_SHAPE"
    assert classify_failure(None).code == "BAD_SHAPE"
    assert classify_failure({"status": "failure"}).code == "UNKNOWN"


# ---------------------------------------------------------- unwrap_payload

def test_unwrap_payload_handles_flat_single_and_double_nesting():
    assert unwrap_payload({"data": DATES}) == DATES
    assert unwrap_payload({"data": {"data": DATES}}) == DATES
    assert unwrap_payload({"data": {"data": CHAIN_INNER}},
                          inner_marker="oc") == CHAIN_INNER
    assert unwrap_payload({"data": CHAIN_INNER},
                          inner_marker="oc") == CHAIN_INNER


def test_unwrap_payload_marker_stops_an_overeager_unwrap():
    # a legitimate payload that HAPPENS to contain a "data" key must not
    # be unwrapped past the marker level
    payload = {"oc": {}, "data": {"nested": True}}
    assert unwrap_payload({"data": payload}, inner_marker="oc") == payload


def test_unwrap_payload_none_on_garbage():
    assert unwrap_payload(None) is None
    assert unwrap_payload("nope") is None
    assert unwrap_payload({}) is None


# ------------------------------------------------------------ expiry list

def test_safe_expiry_list_flat_and_double_nested_success():
    for resp in ({"status": "success", "data": DATES},
                 {"status": "success", "data": {"data": DATES,
                                                "status": "success"}}):
        p1, p2, p3 = _patched()
        with p1, p2 as client, p3:
            client.return_value.expiry_list.return_value = resp
            safe = SafeDhanClient()
            assert safe.get_expiry_list("NIFTY 50") == DATES
            assert safe.last_error is None


def test_safe_expiry_list_classifies_dh906_and_returns_empty():
    p1, p2, p3 = _patched()
    with p1, p2 as client, p3:
        client.return_value.expiry_list.return_value = DH906_RESPONSE
        safe = SafeDhanClient()
        assert safe.get_expiry_list("NIFTY 50") == []
        assert safe.last_error.code == "DH-906"
        assert safe.last_error.endpoint == "expiry_list"
        assert safe.audit and safe.audit[-1]["auth_error"]


def test_safe_expiry_list_strict_mode_raises():
    p1, p2, p3 = _patched()
    with p1, p2 as client, p3:
        client.return_value.expiry_list.return_value = DH906_RESPONSE
        safe = SafeDhanClient(strict=True)
        try:
            safe.get_expiry_list("NIFTY 50")
            assert False, "strict mode must raise on DH-906"
        except DhanApiError as e:
            assert e.code == "DH-906"


def test_safe_expiry_list_survives_a_raising_sdk_call():
    p1, p2, p3 = _patched()
    with p1, p2 as client, p3:
        client.return_value.expiry_list.side_effect = RuntimeError("boom")
        safe = SafeDhanClient()
        assert safe.get_expiry_list("NIFTY 50") == []
        assert safe.last_error.code == "TRANSPORT"


def test_safe_expiry_list_unmapped_ticker_is_classified():
    with mock.patch.object(dc, "_resolve", return_value=None):
        safe = SafeDhanClient()
        assert safe.get_expiry_list("NOT_A_TICKER") == []
        assert safe.last_error.code == "UNMAPPED"


# ------------------------------------------------------------ option chain

def test_safe_option_chain_double_nested_success():
    p1, p2, p3 = _patched()
    with p1, p2 as client, p3:
        client.return_value.option_chain.return_value = {
            "status": "success", "data": {"data": CHAIN_INNER}}
        safe = SafeDhanClient()
        assert safe.get_option_chain("NIFTY 50", "2026-07-21") == CHAIN_INNER


def test_safe_option_chain_dh906_returns_none_with_audit():
    p1, p2, p3 = _patched()
    with p1, p2 as client, p3:
        client.return_value.option_chain.return_value = DH906_RESPONSE
        safe = SafeDhanClient()
        assert safe.get_option_chain("NIFTY 50", "2026-07-21") is None
        assert safe.last_error.code == "DH-906"


def test_safe_option_chain_missing_oc_is_bad_shape_not_a_crash():
    p1, p2, p3 = _patched()
    with p1, p2 as client, p3:
        client.return_value.option_chain.return_value = {
            "status": "success", "data": {"data": {"last_price": 1.0}}}
        safe = SafeDhanClient()
        assert safe.get_option_chain("NIFTY 50", "2026-07-21") is None
        assert safe.last_error.code == "BAD_SHAPE"


# ------------------------------------------------------------ quote / price

def _quote_resp(nested: bool) -> dict:
    inner = {"IDX_I": {"13": QUOTE_SEC}}
    return {"status": "success",
            "data": {"data": inner} if nested else inner}


def test_safe_get_quote_flat_and_double_nested():
    for nested in (False, True):
        p1, p2, p3 = _patched()
        with p1, p2 as client, p3:
            client.return_value.quote_data.return_value = _quote_resp(nested)
            safe = SafeDhanClient()
            q = safe.get_quote("NIFTY 50")
            assert q == {"ticker": "NIFTY 50", "current_price": 3145.0,
                         "prev_close": 3120.0, "percent_change": 0.8}


def test_safe_get_live_price_and_dh906():
    p1, p2, p3 = _patched()
    with p1, p2 as client, p3:
        client.return_value.quote_data.return_value = _quote_resp(True)
        assert SafeDhanClient().get_live_price("NIFTY 50") == 3145.0
    p1, p2, p3 = _patched()
    with p1, p2 as client, p3:
        client.return_value.quote_data.return_value = DH906_RESPONSE
        safe = SafeDhanClient()
        assert safe.get_live_price("NIFTY 50") is None
        assert safe.last_error.code == "DH-906"


# ------------------------------------------------------------- daily bars

def test_safe_ohlc_since_parses_flat_and_double_nested_bars():
    arrays = {"timestamp": [1751932800, 1752019200],
              "open": [100.0, 102.0], "high": [103.0, 104.0],
              "low": [99.0, 101.0], "close": [102.0, 103.5],
              "volume": [1000, 1100]}
    for data in (arrays, {"data": arrays}):
        p1, p2, p3 = _patched()
        with p1, p2 as client, p3:
            client.return_value.historical_daily_data.return_value = {
                "status": "success", "data": data}
            bars = SafeDhanClient().get_ohlc_since("NIFTY 50", "2020-01-01")
            assert len(bars) == 2
            assert bars[0]["close"] == 102.0 and bars[1]["high"] == 104.0
            assert all(k in bars[0] for k in
                       ("date", "open", "high", "low", "close", "volume"))


def test_safe_ohlc_since_dh906_returns_empty_with_audit():
    p1, p2, p3 = _patched()
    with p1, p2 as client, p3:
        client.return_value.historical_daily_data.return_value = DH906_RESPONSE
        safe = SafeDhanClient()
        assert safe.get_ohlc_since("NIFTY 50", "2020-01-01") == []
        assert safe.last_error.code == "DH-906"


def test_safe_ohlc_since_same_day_guard_never_calls_the_sdk():
    from datetime import date
    p1, p2, p3 = _patched()
    with p1, p2 as client, p3:
        safe = SafeDhanClient()
        assert safe.get_ohlc_since("NIFTY 50", date.today().isoformat()) == []
        assert not client.return_value.historical_daily_data.called


# ---------------------------------------------------------------- audit view

def test_auth_failures_filters_token_errors_from_shape_noise():
    p1, p2, p3 = _patched()
    with p1, p2 as client, p3:
        safe = SafeDhanClient()
        client.return_value.expiry_list.return_value = DH906_RESPONSE
        safe.get_expiry_list("NIFTY 50")
        client.return_value.expiry_list.return_value = {
            "status": "success", "data": {"data": "not-a-list"}}
        safe.get_expiry_list("NIFTY 50")
        assert len(safe.audit) == 2
        auth = safe.auth_failures()
        assert len(auth) == 1 and auth[0]["code"] == "DH-906"


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
