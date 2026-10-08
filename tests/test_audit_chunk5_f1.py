"""Audit Chunk 5 F1: SafeDhanClient reserves the host-wide throttle slot like every other Dhan caller."""
from src import dhan_client as dc, dhan_guard as dg


def test_f1_safe_client_calls_reserve_the_throttle(monkeypatch):
    seen = []
    monkeypatch.setattr(dc, "_throttle", lambda chain=False: seen.append(chain))
    monkeypatch.setattr(dc, "_RATE_PAUSE", 0.0)
    c = dg.SafeDhanClient.__new__(dg.SafeDhanClient)
    resp, err = c._call("option_chain", lambda: {"status": "success", "data": {}})
    assert err is None and seen == [True]
    resp, err = c._call("quote_data", lambda: {"status": "failure", "remarks": {"error_code": "DH-904"}})
    assert err is not None and seen == [True, False, False]        # one retry, both throttled
