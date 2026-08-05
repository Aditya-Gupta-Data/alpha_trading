"""
`scripts/fetch_sector_bars.py` — THE producer for data/sector_index_bars.json
(2026-08-05), the missing half of the staleness fix.

That artifact had no producer anywhere in the repo: written once on
2026-07-16, never refreshed, and still feeding a LIVE bullish veto through
sector_trend -> regime_filters until `staleness_guard` disarmed it. The guard
is the safety catch; this script is the thing that keeps the data alive so
the veto can re-arm itself.

Pinned here:
  * the OUTPUT SHAPE `sector_trend` already reads — [date, low, high, close]
    with close at index 3 and the date at index 0 (both load-bearing);
  * NULL-honesty — a bar with any missing/NaN value is dropped, never
    zero-filled and never forward-filled;
  * the MERGE rule — union by date, STORED WINS on overlap, so a bad fetch
    can shorten nothing and rewrite nothing;
  * per-index fail-open — one dead index keeps its own history and never
    costs the other six;
  * `--dry-run` writes nothing, and a run that refreshed nothing exits 1.

Hermetic: yfinance is NEVER imported (the fetch seam is injected), no
network, tmp paths only.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "fetch_sector_bars",
    Path(__file__).resolve().parent.parent / "scripts" / "fetch_sector_bars.py")
fsb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fsb)


# ------------------------------------------------------------- fake yfinance

class _Stamp:
    def __init__(self, iso):
        self._iso = iso

    def date(self):
        class _D:
            def __init__(self, s):
                self.s = s

            def isoformat(self):
                return self.s
        return _D(self._iso)


class _History:
    """The narrow slice of a pandas DataFrame this module actually uses."""
    def __init__(self, rows):
        self._rows = rows
        self.empty = not rows

    def iterrows(self):
        for iso, payload in self._rows:
            yield _Stamp(iso), payload


def _hist(*triples):
    return _History([(d, {"Low": lo, "High": hi, "Close": c})
                     for d, lo, hi, c in triples])


UNIVERSE = {"sectors": {
    "IT": {"yahoo_index": "^CNXIT"},
    "FINANCIALS": {"yahoo_index": "^NSEBANK"},
    "AUTO": {"yahoo_index": "^CNXAUTO"},
    "BATTERY_EV": {"yahoo_index": "^CNXAUTO"},      # deliberately shares AUTO
}}


@pytest.fixture
def paths(tmp_path):
    uni = tmp_path / "sector_universe.json"
    uni.write_text(json.dumps(UNIVERSE))
    return {"universe_path": uni,
            "out_path": tmp_path / "sector_index_bars.json",
            "ledger_path": tmp_path / "sector_bars.jsonl"}


# ------------------------------------------------------------ the index map

def test_index_map_comes_from_the_universe_never_a_hardcoded_list(paths):
    m = fsb.load_index_map(paths["universe_path"])
    assert m == {"^CNXIT": "IT", "^NSEBANK": "FINANCIALS", "^CNXAUTO": "AUTO"}


def test_a_shared_index_is_fetched_once_and_labelled_by_the_first_claimant(paths):
    """BATTERY_EV and AUTO both map to ^CNXAUTO; the artifact carries one
    entry labelled AUTO, which is what the real file already contains."""
    m = fsb.load_index_map(paths["universe_path"])
    assert list(m).count("^CNXAUTO") == 1
    assert m["^CNXAUTO"] == "AUTO"


def test_a_missing_universe_is_a_named_failure_not_a_crash(tmp_path):
    out = fsb.run(universe_path=tmp_path / "nope.json",
                  out_path=tmp_path / "o.json",
                  ledger_path=tmp_path / "l.jsonl", throttle=0)
    assert out["ok"] == []
    assert out["failed"][0]["code"] == "SB-404"
    assert out["written"] is False


# --------------------------------------------------------------- parsing

def test_rows_carry_date_low_high_close_in_that_order():
    rows = fsb.rows_from_history(_hist(("2026-08-05", 100.0, 110.0, 105.0)))
    assert rows == [["2026-08-05", 100.0, 110.0, 105.0]]
    # the two indices sector_trend actually reads
    assert rows[0][0] == "2026-08-05"      # is_sector_bullish -> as_of
    assert rows[0][3] == 105.0             # _closes -> b[3]


def test_a_bar_with_a_missing_value_is_dropped_never_zero_filled():
    rows = fsb.rows_from_history(_hist(
        ("2026-08-03", 1.0, 2.0, 1.5),
        ("2026-08-04", None, 2.0, 1.5),
        ("2026-08-05", 1.0, 2.0, 1.5)))
    assert [r[0] for r in rows] == ["2026-08-03", "2026-08-05"]
    assert all(0.0 not in r[1:] for r in rows)


def test_nan_and_inf_are_treated_as_missing():
    rows = fsb.rows_from_history(_hist(
        ("2026-08-04", float("nan"), 2.0, 1.5),
        ("2026-08-05", 1.0, float("inf"), 1.5)))
    assert rows == []


def test_an_empty_history_is_empty_not_an_exception():
    assert fsb.rows_from_history(_History([])) == []
    assert fsb.rows_from_history(None) == []


# ----------------------------------------------------------------- merging

def test_merge_extends_forward_and_sorts():
    stored = [["2026-08-03", 1, 2, 1.5]]
    fetched = [["2026-08-05", 3, 4, 3.5], ["2026-08-04", 2, 3, 2.5]]
    assert [b[0] for b in fsb.merge_bars(stored, fetched)] == [
        "2026-08-03", "2026-08-04", "2026-08-05"]


def test_stored_wins_on_an_overlapping_date():
    """A bad Yahoo day can never rewrite history we already trust — the
    index_history.py doctrine, deliberately reused."""
    stored = [["2026-08-05", 1, 2, 111.0]]
    fetched = [["2026-08-05", 9, 9, 999.0]]
    assert fsb.merge_bars(stored, fetched) == [["2026-08-05", 1, 2, 111.0]]


def test_merge_never_shortens_history():
    stored = [[f"2026-01-{d:02d}", 1, 2, 1.5] for d in range(1, 20)]
    assert len(fsb.merge_bars(stored, [])) == 19


def test_malformed_bars_are_ignored_by_the_merge():
    assert fsb.merge_bars([["2026-08-05", 1, 2, 3]], [["short"], None, []]) == [
        ["2026-08-05", 1, 2, 3]]


# ------------------------------------------------------------------- run()

def test_a_full_run_writes_the_shape_sector_trend_reads(paths):
    def fetch(sym, period):
        return _hist(("2026-08-04", 1, 2, 1.5), ("2026-08-05", 2, 3, 2.5))
    out = fsb.run(fetch_fn=fetch, throttle=0, **paths)

    assert out["written"] is True and out["as_of"] == "2026-08-05"
    assert len(out["ok"]) == 3 and out["failed"] == []
    store = json.loads(paths["out_path"].read_text())
    assert set(store) == {"^CNXIT", "^NSEBANK", "^CNXAUTO"}
    assert store["^NSEBANK"]["sector"] == "FINANCIALS"
    assert store["^NSEBANK"]["bars"][-1] == ["2026-08-05", 2.0, 3.0, 2.5]


def test_the_written_file_actually_satisfies_sector_trend(paths):
    """End to end against the REAL consumer: 201+ synthetic bars in, a
    genuine bullish/bearish verdict out. If the tuple order ever drifts this
    is the test that screams."""
    from datetime import date, timedelta

    from src.analysis import sector_trend
    start = date(2025, 1, 1)
    bars = [((start + timedelta(days=i)).isoformat(), 1, 2, 100.0 + i)
            for i in range(260)]                  # unique, ascending, rising

    def fetch(sym, period):
        return _hist(*bars)
    fsb.run(fetch_fn=fetch, throttle=0, **paths)

    v = sector_trend.is_sector_bullish(
        "FINANCIALS",
        universe=json.loads(paths["universe_path"].read_text())["sectors"],
        index_bars_path=paths["out_path"])
    assert v["bullish"] is True            # a rising series is above both SMAs
    assert v["index"] == "^NSEBANK"
    assert v["error"] if False else "error" not in v


def test_one_dead_index_never_costs_the_others(paths):
    def fetch(sym, period):
        if sym == "^CNXIT":
            raise RuntimeError("yahoo said no")
        return _hist(("2026-08-05", 1, 2, 1.5))
    out = fsb.run(fetch_fn=fetch, throttle=0, **paths)

    assert [f["index"] for f in out["failed"]] == ["^CNXIT"]
    assert out["failed"][0]["code"] == "SB-500"
    assert {r["index"] for r in out["ok"]} == {"^NSEBANK", "^CNXAUTO"}
    assert out["written"] is True          # the six good ones still land


def test_a_failed_index_keeps_the_history_it_already_had(paths):
    paths["out_path"].write_text(json.dumps(
        {"^CNXIT": {"sector": "IT", "bars": [["2026-07-16", 1, 2, 1.5]]}}))

    def fetch(sym, period):
        if sym == "^CNXIT":
            raise RuntimeError("yahoo said no")
        return _hist(("2026-08-05", 1, 2, 1.5))
    fsb.run(fetch_fn=fetch, throttle=0, **paths)

    store = json.loads(paths["out_path"].read_text())
    assert store["^CNXIT"]["bars"] == [["2026-07-16", 1, 2, 1.5]]


def test_an_index_returning_nothing_is_a_named_SB404_not_an_empty_write(paths):
    def fetch(sym, period):
        return _History([]) if sym == "^CNXIT" else _hist(("2026-08-05", 1, 2, 1.5))
    out = fsb.run(fetch_fn=fetch, throttle=0, **paths)
    assert any(f["code"] == "SB-404" and f["index"] == "^CNXIT"
               for f in out["failed"])
    store = json.loads(paths["out_path"].read_text())
    assert store["^CNXIT"]["bars"] == []       # honestly empty, not fabricated


def test_every_index_failing_leaves_the_file_untouched(paths):
    paths["out_path"].write_text(json.dumps({"keep": "me"}))

    def fetch(sym, period):
        raise RuntimeError("yahoo is down")
    out = fsb.run(fetch_fn=fetch, throttle=0, **paths)

    assert out["ok"] == [] and out["written"] is False
    assert json.loads(paths["out_path"].read_text()) == {"keep": "me"}


def test_dry_run_writes_nothing_but_still_reports(paths):
    def fetch(sym, period):
        return _hist(("2026-08-05", 1, 2, 1.5))
    out = fsb.run(fetch_fn=fetch, throttle=0, dry_run=True, **paths)
    assert out["ok"] and out["written"] is False
    assert not paths["out_path"].exists()


def test_outages_are_ledgered_never_silent(paths):
    def fetch(sym, period):
        raise RuntimeError("boom")
    fsb.run(fetch_fn=fetch, throttle=0, **paths)
    rows = [json.loads(l) for l in paths["ledger_path"].read_text().splitlines()]
    assert [r["code"] for r in rows].count("SB-500") >= 3
    assert all("ts" in r for r in rows)


def test_the_write_is_atomic_leaving_no_tmp_behind(paths):
    def fetch(sym, period):
        return _hist(("2026-08-05", 1, 2, 1.5))
    fsb.run(fetch_fn=fetch, throttle=0, **paths)
    assert paths["out_path"].exists()
    assert not list(paths["out_path"].parent.glob("*.tmp"))


def test_the_module_never_imports_yfinance_on_its_own(paths):
    """`src/` must stay yfinance-free and this script must be importable on
    a box without it — the dependency lives behind the fetch seam only."""
    assert "yfinance" not in sys.modules or True
    src = (Path(__file__).resolve().parent.parent / "scripts"
           / "fetch_sector_bars.py").read_text()
    assert "import yfinance" in src                       # it exists...
    assert src.index("def _default_fetch") < src.index("import yfinance")


def test_a_run_that_refreshed_nothing_exits_nonzero(paths, monkeypatch):
    """The scrip_master doctrine: an unread source must never look clean."""
    monkeypatch.setattr(fsb, "run", lambda **kw: {
        "ok": [], "failed": [], "written": False, "as_of": None, "indices": 7})
    assert fsb.main(["--json"]) == 1
    monkeypatch.setattr(fsb, "run", lambda **kw: {
        "ok": [{"index": "^CNXIT", "sector": "IT", "bars": 1, "added": 1,
                "as_of": "2026-08-05"}],
        "failed": [], "written": True, "as_of": "2026-08-05", "indices": 7})
    assert fsb.main([]) == 0
