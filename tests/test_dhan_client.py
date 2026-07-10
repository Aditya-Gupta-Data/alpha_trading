"""
Tests for src/dhan_client.py's response-shape unwrapping — offline,
mocking the SDK client so no network/token is ever needed.

Run from the project folder:
    python tests/test_dhan_client.py      (simple, no extra installs)
    python -m pytest tests/               (if you have pytest)
"""

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import dhan_client as dc

DATES = ["2026-07-14", "2026-07-21", "2026-07-28"]


def _with_client(resolved=True):
    """Patch _resolve/_get_client so get_expiry_list reaches the SDK call."""
    instr = {"id": "13", "seg": "IDX_I"} if resolved else None
    return (mock.patch.object(dc, "_resolve", return_value=instr),
            mock.patch.object(dc, "_get_client", return_value=mock.Mock()))


def test_get_expiry_list_unwraps_the_doubly_nested_shape():
    """Regression (found live 2026-07-09): Dhan's real SDK response is
    {"status", "data": {"data": [...], "status": ...}} — double nested.
    The single-unwrap version silently handed pick_expiry a dict, which
    iterated its KEYS as dates and matched nothing: every proposal cycle
    failed with 'no usable expiry' regardless of real market state."""
    resp = {"status": "success", "remarks": "",
            "data": {"data": DATES, "status": "success"}}
    p1, p2 = _with_client()
    with p1, p2 as get_client:
        get_client.return_value.expiry_list.return_value = resp
        assert dc.get_expiry_list("NIFTY 50") == DATES


def test_get_expiry_list_still_handles_a_single_nested_shape():
    """Backward compatible: if Dhan ever reverts to one layer, it still
    works — never assume the wire format can't drift again."""
    resp = {"status": "success", "data": DATES}
    p1, p2 = _with_client()
    with p1, p2 as get_client:
        get_client.return_value.expiry_list.return_value = resp
        assert dc.get_expiry_list("NIFTY 50") == DATES


def test_get_expiry_list_degrades_to_empty_on_garbage_shapes():
    for resp in (None, {}, {"data": None}, {"data": {"data": "not-a-list"}},
                 {"data": 42}, "not even a dict"):
        p1, p2 = _with_client()
        with p1, p2 as get_client:
            get_client.return_value.expiry_list.return_value = resp
            assert dc.get_expiry_list("NIFTY 50") == []


def test_get_expiry_list_empty_when_ticker_or_client_unresolved():
    p1, p2 = _with_client(resolved=False)
    with p1, p2:
        assert dc.get_expiry_list("NOT_A_REAL_TICKER") == []


def test_get_expiry_list_survives_a_raising_sdk_call():
    p1, p2 = _with_client()
    with p1, p2 as get_client:
        get_client.return_value.expiry_list.side_effect = RuntimeError("boom")
        assert dc.get_expiry_list("NIFTY 50") == []


CHAIN_INNER = {"last_price": 24065.2, "oc": {"24000.000000": {
    "ce": {"last_price": 120.5}, "pe": {"last_price": 95.0}}}}


def test_get_option_chain_unwraps_the_doubly_nested_shape():
    """Regression (found live 2026-07-09, right after the identical
    get_expiry_list bug): Dhan's option_chain response is ALSO doubly
    nested — {"data": {"data": {"last_price", "oc"}}} — the single
    unwrap handed options_proposer a dict with no "oc" key, so every
    chain read failed with 'option chain unavailable'."""
    resp = {"status": "success", "remarks": "",
            "data": {"data": CHAIN_INNER}}
    p1, p2 = _with_client()
    with p1, p2 as get_client:
        get_client.return_value.option_chain.return_value = resp
        chain = dc.get_option_chain("NIFTY 50", "2026-07-14")
        assert chain == CHAIN_INNER
        assert chain["oc"]


def test_get_option_chain_still_handles_a_single_nested_shape():
    resp = {"status": "success", "data": CHAIN_INNER}
    p1, p2 = _with_client()
    with p1, p2 as get_client:
        get_client.return_value.option_chain.return_value = resp
        assert dc.get_option_chain("NIFTY 50", "2026-07-14") == CHAIN_INNER


def test_get_option_chain_none_on_failure_status_or_garbage():
    for resp in ({"status": "failure", "data": CHAIN_INNER}, None,
                 {"status": "success", "data": None},
                 {"status": "success", "data": {"data": "not-a-dict"}}):
        p1, p2 = _with_client()
        with p1, p2 as get_client:
            get_client.return_value.option_chain.return_value = resp
            assert dc.get_option_chain("NIFTY 50", "2026-07-14") is None


# --- 2026-07-10 audit: the remaining parsers, same latent double-nesting ---

BAR_ARRAYS = {"timestamp": [1751932800, 1752019200],
              "open": [100.0, 102.0], "high": [103.0, 104.0],
              "low": [99.0, 101.0], "close": [102.0, 103.5],
              "volume": [1000, 1100]}


def _instr_patch():
    instr = {"id": "13", "seg": "IDX_I", "inst": "INDEX"}
    return (mock.patch.object(dc, "_resolve", return_value=instr),
            mock.patch.object(dc, "_get_client", return_value=mock.Mock()))


def test_get_daily_ohlc_unwraps_a_doubly_nested_historical_payload():
    """Audit finding (planning_index §1.2): _fetch_daily read
    resp["data"] one layer deep only — a doubly-nested payload made
    d.get("timestamp") come back empty, so bars silently degraded to []
    and looked exactly like a market data gap."""
    resp = {"status": "success", "data": {"data": BAR_ARRAYS}}
    p1, p2 = _instr_patch()
    with p1, p2 as get_client:
        get_client.return_value.historical_daily_data.return_value = resp
        bars = dc.get_daily_ohlc("NIFTY 50", days=5)
        assert len(bars) == 2
        assert bars[0]["close"] == 102.0 and bars[1]["close"] == 103.5


def test_get_daily_ohlc_still_handles_the_flat_shape():
    resp = {"status": "success", "data": BAR_ARRAYS}
    p1, p2 = _instr_patch()
    with p1, p2 as get_client:
        get_client.return_value.historical_daily_data.return_value = resp
        assert len(dc.get_daily_ohlc("NIFTY 50", days=5)) == 2


def test_get_daily_ohlc_empty_on_garbage_inner_shapes():
    for data in ("", None, {"data": "not-a-dict"}, {"data": None}):
        resp = {"status": "success", "data": data}
        p1, p2 = _instr_patch()
        with p1, p2 as get_client:
            get_client.return_value.historical_daily_data.return_value = resp
            assert dc.get_daily_ohlc("NIFTY 50", days=5) == []


QUOTE_SEC = {"last_price": 3145.0, "ohlc": {"close": 3120.0}}


def test_get_quote_handles_double_and_single_nested_payloads():
    """_quote_sec hardcoded resp["data"]["data"][seg][id]: correct for
    today's doubly-nested wire shape, but a single-nested revert would
    have silently returned None for every quote. Both now work."""
    inner = {"IDX_I": {"13": QUOTE_SEC}}
    for data in ({"data": inner}, inner):
        resp = {"status": "success", "data": data}
        p1, p2 = _instr_patch()
        with p1, p2 as get_client:
            get_client.return_value.quote_data.return_value = resp
            q = dc.get_quote("NIFTY 50")
            assert q is not None
            assert q["current_price"] == 3145.0
            assert q["prev_close"] == 3120.0


def test_get_quote_none_on_failure_and_garbage(monkeypatch=None):
    for resp in ({"status": "failure", "remarks": {"error_code": "DH-906"}},
                 {"status": "success", "data": None},
                 {"status": "success", "data": {"data": "nope"}}):
        p1, p2 = _instr_patch()
        with p1, p2 as get_client, mock.patch.object(dc, "_RATE_PAUSE", 0):
            get_client.return_value.quote_data.return_value = resp
            assert dc.get_quote("NIFTY 50") is None


def test_pick_expiry_actually_picks_from_a_realistic_expiry_list():
    """End-to-end proof the fix restores real proposal flow: with the
    unwrapped list, pick_expiry finds the first date >= MIN_DAYS_TO_EXPIRY
    out — exactly the step that silently returned None all day."""
    from datetime import date
    from src.options_proposer import MIN_DAYS_TO_EXPIRY, pick_expiry
    today = date(2026, 7, 9)   # a Thursday; nearest listed expiry is 5d out
    picked = pick_expiry(DATES, today=today)
    assert picked == "2026-07-21"
    assert (date.fromisoformat(picked) - today).days >= MIN_DAYS_TO_EXPIRY


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
