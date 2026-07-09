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
