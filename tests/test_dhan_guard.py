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


# --- Phase 5: the API deception guard (stale-data rejection) ----------------

from datetime import datetime, timedelta, timezone

from src.dhan_guard import (StaleDataError, freshness_error,
                            parse_market_timestamp)

_IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN_NOW = datetime(2026, 7, 10, 11, 0, 0, tzinfo=_IST)   # Friday 11:00
MARKET_CLOSED_NOW = datetime(2026, 7, 11, 11, 0, 0, tzinfo=_IST)  # Saturday

FRESH_TS = "2026-07-10 10:59:30"    # 30s old at 11:00
STALE_TS = "2026-07-10 10:57:00"    # 180s old at 11:00


def test_parse_market_timestamp_formats():
    epoch = MARKET_OPEN_NOW.timestamp()
    assert parse_market_timestamp(epoch) == MARKET_OPEN_NOW
    assert parse_market_timestamp(epoch * 1000) == MARKET_OPEN_NOW  # millis
    assert parse_market_timestamp(str(int(epoch))) == MARKET_OPEN_NOW
    parsed = parse_market_timestamp("2026-07-10 10:57:00")
    assert parsed == datetime(2026, 7, 10, 10, 57, 0, tzinfo=_IST)
    assert parse_market_timestamp("2026-07-10T10:57:00+05:30") == parsed
    for garbage in (None, "", "not a time", {}, -5, 0):
        assert parse_market_timestamp(garbage) is None


def test_freshness_error_fires_only_on_stale_data_during_market_hours():
    stale = freshness_error({"last_trade_time": STALE_TS, "last_price": 1.0},
                            endpoint="quote_data", max_age=60,
                            now=MARKET_OPEN_NOW)
    assert isinstance(stale, StaleDataError)
    assert stale.code == "STALE_DATA"
    assert stale.age_seconds == 180.0
    assert not stale.is_auth_error
    # fresh data passes
    assert freshness_error({"last_trade_time": FRESH_TS}, max_age=60,
                           now=MARKET_OPEN_NOW) is None
    # the same stale timestamp OUTSIDE market hours is legitimate (the close)
    assert freshness_error({"last_trade_time": STALE_TS}, max_age=60,
                           now=MARKET_CLOSED_NOW) is None
    # no recognizable timestamp field = unverifiable, never punished
    assert freshness_error({"last_price": 1.0}, now=MARKET_OPEN_NOW) is None
    assert freshness_error("not a dict", now=MARKET_OPEN_NOW) is None


def test_freshness_error_reads_alternate_field_names():
    for field in ("lastTradeTime", "ltt", "exchange_time", "timestamp"):
        err = freshness_error({field: STALE_TS}, max_age=60,
                              now=MARKET_OPEN_NOW)
        assert err is not None and err.code == "STALE_DATA", field


def _stale_quote_resp():
    sec = dict(QUOTE_SEC, last_trade_time=STALE_TS)
    return {"status": "success", "data": {"data": {"IDX_I": {"13": sec}}}}


def test_safe_quote_rejects_stale_snapshots_mid_session():
    p1, p2, p3 = _patched()
    with p1, p2 as client, p3:
        client.return_value.quote_data.return_value = _stale_quote_resp()
        safe = SafeDhanClient(now_fn=lambda: MARKET_OPEN_NOW)
        assert safe.get_quote("NIFTY 50") is None
        assert safe.last_error.code == "STALE_DATA"
        assert safe.last_error.endpoint == "quote_data"
        assert "180s old" in safe.last_error.message


def test_safe_quote_strict_mode_raises_stale_data_error():
    p1, p2, p3 = _patched()
    with p1, p2 as client, p3:
        client.return_value.quote_data.return_value = _stale_quote_resp()
        safe = SafeDhanClient(strict=True, now_fn=lambda: MARKET_OPEN_NOW)
        try:
            safe.get_quote("NIFTY 50")
            assert False, "strict mode must raise on stale data"
        except StaleDataError as e:
            assert e.age_seconds == 180.0


def test_safe_quote_accepts_fresh_and_off_hours_data():
    fresh = dict(QUOTE_SEC, last_trade_time=FRESH_TS)
    resp = {"status": "success", "data": {"data": {"IDX_I": {"13": fresh}}}}
    p1, p2, p3 = _patched()
    with p1, p2 as client, p3:
        client.return_value.quote_data.return_value = resp
        safe = SafeDhanClient(now_fn=lambda: MARKET_OPEN_NOW)
        assert safe.get_quote("NIFTY 50")["current_price"] == 3145.0
    # the stale snapshot is fine when the market is closed (it IS the close)
    p1, p2, p3 = _patched()
    with p1, p2 as client, p3:
        client.return_value.quote_data.return_value = _stale_quote_resp()
        safe = SafeDhanClient(now_fn=lambda: MARKET_CLOSED_NOW)
        assert safe.get_quote("NIFTY 50") is not None


def test_safe_quote_threshold_is_configurable():
    p1, p2, p3 = _patched()
    with p1, p2 as client, p3:
        client.return_value.quote_data.return_value = _stale_quote_resp()
        lenient = SafeDhanClient(max_quote_age_seconds=300,
                                 now_fn=lambda: MARKET_OPEN_NOW)
        assert lenient.get_quote("NIFTY 50") is not None   # 180s < 300s


def test_safe_option_chain_rejects_stale_and_passes_untimestamped():
    stale_chain = dict(CHAIN_INNER, last_trade_time=STALE_TS)
    p1, p2, p3 = _patched()
    with p1, p2 as client, p3:
        client.return_value.option_chain.return_value = {
            "status": "success", "data": {"data": stale_chain}}
        safe = SafeDhanClient(now_fn=lambda: MARKET_OPEN_NOW)
        assert safe.get_option_chain("NIFTY 50", "2026-07-21") is None
        assert safe.last_error.code == "STALE_DATA"
        assert safe.last_error.endpoint == "option_chain"
    # a chain payload with no timestamp field keeps working (unverifiable)
    p1, p2, p3 = _patched()
    with p1, p2 as client, p3:
        client.return_value.option_chain.return_value = {
            "status": "success", "data": {"data": CHAIN_INNER}}
        safe = SafeDhanClient(now_fn=lambda: MARKET_OPEN_NOW)
        assert safe.get_option_chain("NIFTY 50", "2026-07-21") == CHAIN_INNER


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



# --------------------------------------- 2026-07-10 refinement regressions

def test_freshness_error_ignores_implausibly_old_stamps():
    """A mid-session stamp HOURS old means a miszoned/misparsed field
    (e.g. an epoch already expressed in IST wall-clock), not a feed that
    is genuinely half a day behind — treated like no timestamp at all,
    never a blanket StaleDataError across every quote."""
    four_hours_old = "2026-07-10 07:00:00"
    assert freshness_error({"last_trade_time": four_hours_old},
                           max_age=60, now=MARKET_OPEN_NOW) is None


def test_equity_quotes_skip_the_freshness_guard():
    """The staleness guard is for indexes (they tick every second); an
    individual equity legitimately goes minutes without a trade and must
    still mark — a stale-stamped equity quote passes through."""
    equity_instr = {"id": "11536", "seg": "NSE_EQ", "inst": "EQUITY"}
    sec = dict(QUOTE_SEC, last_trade_time=STALE_TS)
    resp = {"status": "success",
            "data": {"data": {"NSE_EQ": {"11536": sec}}}}
    p1, p2, p3 = _patched(instr=equity_instr)
    with p1, p2 as client, p3:
        client.return_value.quote_data.return_value = resp
        safe = SafeDhanClient(now_fn=lambda: MARKET_OPEN_NOW)
        assert safe.get_live_price("TCS.NS") == 3145.0
        assert safe.last_error is None


def test_safe_ohlc_since_ragged_arrays_degrade_never_raise():
    """A truncated OHLC array yields fewer bars — never an IndexError out
    of a class whose documented contract is empty-state-on-failure."""
    ragged = {"timestamp": [1751932800, 1752019200, 1752105600],
              "open": [100.0, 102.0], "high": [103.0, 104.0],
              "low": [99.0, 101.0], "close": [102.0, 103.5],
              "volume": []}
    p1, p2, p3 = _patched()
    with p1, p2 as client, p3:
        client.return_value.historical_daily_data.return_value = {
            "status": "success", "data": ragged}
        bars = SafeDhanClient().get_ohlc_since("NIFTY 50", "2020-01-01")
    assert len(bars) == 2                      # shortest array wins
    assert bars[1]["volume"] == 0.0            # missing volume defaults


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


# --------------------------------------------------------------------------
# DH-905 is an INPUT error, not an auth error (corrected 2026-07-20)
# --------------------------------------------------------------------------

def test_dh905_is_an_input_error_not_a_dead_token():
    """The 2026-07-20 CEO brief filed 14 DH-905s under a credential alarm.
    Dhan's own payload calls it Input_Exception — a bad question, not a bad
    token. Paging the owner to rotate a healthy token is the harm here."""
    err = classify_failure({
        "status": "failure",
        "remarks": {"error_code": "DH-905", "error_type": "Input_Exception",
                    "error_message": "Invalid date range"},
        "data": ""})
    assert err.code == "DH-905"
    assert not err.is_auth_error       # the whole point
    assert err.is_input_error
    assert err.as_dict()["auth_error"] is False
    assert err.as_dict()["input_error"] is True


def test_real_auth_codes_still_page():
    """Guard the correction from over-reaching: DH-901/DH-906 must still
    read as credential failures."""
    for code in ("DH-901", "DH-906"):
        err = classify_failure({"status": "failure",
                                "remarks": {"error_code": code,
                                            "error_message": "Invalid Token"}})
        assert err.is_auth_error, code
        assert not err.is_input_error, code


def test_dh905_in_a_free_text_remark_is_still_identified():
    """Removing DH-905 from AUTH_ERROR_CODES must not make the string-remark
    parser forget the code exists (it scans KNOWN_ERROR_CODES now)."""
    err = classify_failure({"status": "failure",
                            "remarks": "DH-905 : Renewal of token not allowed"})
    assert err.code == "DH-905"
    assert not err.is_auth_error


def test_auth_and_input_failures_are_separate_audit_views():
    from src.dhan_guard import DhanApiError, SafeDhanClient
    safe = SafeDhanClient()
    safe.audit.append(DhanApiError("DH-906", "Invalid Token", "quote").as_dict())
    safe.audit.append(DhanApiError("DH-905", "bad range", "historical").as_dict())
    assert [a["code"] for a in safe.auth_failures()] == ["DH-906"]
    assert [a["code"] for a in safe.input_failures()] == ["DH-905"]
