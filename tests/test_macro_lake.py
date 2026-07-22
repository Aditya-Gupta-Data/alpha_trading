"""
The macro lake clerk (the cross-asset engine's fuel line), fully offline:
FRED's '.' null stays a visible hole (never 0.0, never a dropped row),
ingest is append-only and idempotent, one dead series never aborts the
sweep and comes back NAMED, a missing API key is a NAMED ML-401 not a
crash, and --dry-run writes nothing.

Every test that fetches passes api_key="TEST-KEY" explicitly, so a real
FRED_API_KEY in .env (or its absence) can never change a result — the
journal-drift isolation lesson, 2026-07-22.
"""
import json

from src.ingestion import macro_lake as ML

KEY = "TEST-KEY"


def _payload(days):
    """Canned FRED API JSON for a list of (date, value-string) pairs."""
    return json.dumps({"observations": [
        {"date": d, "value": v} for d, v in days]}).encode()


DAYS = [("2026-07-14", "84.10"), ("2026-07-15", "."),
        ("2026-07-16", "85.25")]
DAYS_NEXT = DAYS + [("2026-07-17", "86.00")]


def _canned(days=DAYS):
    return lambda url: _payload(days)


def test_dot_parses_to_none_never_zero():
    rows = ML.parse_observations(_payload(DAYS))
    assert [d for d, _ in rows] == ["2026-07-14", "2026-07-15", "2026-07-16"]
    assert rows[1][1] is None            # '.' -> None, and the row SURVIVES
    assert rows[1][1] != 0.0
    assert rows[0][1] == 84.10 and rows[2][1] == 85.25


def test_shapeless_payload_is_honest_empty_not_a_crash():
    assert ML.parse_observations(b"<html>rate limited</html>") == []
    assert ML.parse_observations(b'{"error_message": "Bad API key"}') == []


def test_lake_file_csv_parser_keeps_the_same_null_law():
    """The stored lake format is CSV; its reader honors the same hole
    law as the API parser (read_series round-trips None)."""
    raw = b"date,value\n2026-07-14,84.1\n2026-07-15,\n"
    rows = ML.parse_lake_csv(raw)
    assert rows == [("2026-07-14", 84.1), ("2026-07-15", None)]


def test_fetch_observations_uses_the_api_url_with_key():
    seen = []

    def spy(url):
        seen.append(url)
        return _payload(DAYS)

    out = ML.fetch_observations("DGS10", api_key="k123", fetch_bytes_fn=spy)
    assert out == _payload(DAYS)
    assert seen == ["https://api.stlouisfed.org/fred/series/observations"
                    "?series_id=DGS10&api_key=k123&file_type=json"]


def test_missing_api_key_is_a_named_ml401_not_a_crash(tmp_path, monkeypatch):
    """No key -> every series fails NAMED (ML-401, reason no_api_key) and
    the sweep still RETURNS its summary. _api_key is patched so a real key
    in .env or the environment can never flip this test."""
    monkeypatch.setattr(ML, "_api_key", lambda: None)
    out = ML.ingest_all(sleep_fn=lambda s: None, lake_dir=tmp_path,
                        log_path=tmp_path / "o.jsonl")
    assert out["ok"] == []
    assert sorted(f["series"] for f in out["failed"]) == \
        ["BRENT", "DXY", "US10Y", "USDINR"]
    assert all(f["code"] == "ML-401" for f in out["failed"])
    assert all(f["reason"] == "no_api_key" for f in out["failed"])
    assert all("no_api_key" in f["detail"] for f in out["failed"])
    assert out["rows_added"] == {"BRENT": 0, "DXY": 0, "USDINR": 0,
                                 "US10Y": 0}
    assert list(tmp_path.glob("*.csv")) == []
    assert "ML-401" in (tmp_path / "o.jsonl").read_text()


def test_ingest_is_idempotent_and_stores_holes_honestly(tmp_path):
    first = ML.ingest("BRENT", api_key=KEY, fetch_bytes_fn=_canned(),
                      lake_dir=tmp_path)
    assert first["rows_added"] == 3 and first["holes_added"] == 1
    lake = tmp_path / "BRENT.csv"
    assert lake.read_text().splitlines()[0] == "date,value"
    assert "2026-07-15," in lake.read_text()      # the hole, empty not 0
    assert ML.read_series("BRENT", lake_dir=tmp_path)[1][1] is None

    again = ML.ingest("BRENT", api_key=KEY, fetch_bytes_fn=_canned(),
                      lake_dir=tmp_path)
    assert again["rows_added"] == 0               # second run adds nothing
    assert len(ML.read_series("BRENT", lake_dir=tmp_path)) == 3


def test_history_is_append_only(tmp_path):
    ML.ingest("BRENT", api_key=KEY, fetch_bytes_fn=_canned(),
              lake_dir=tmp_path)
    before = (tmp_path / "BRENT.csv").read_text()

    # upstream revises an OLD day and publishes a new one: the old bytes
    # stay exactly as captured, only the newer date lands
    revised = [("2026-07-14", "99.99")] + DAYS_NEXT[1:]
    out = ML.ingest("BRENT", api_key=KEY, fetch_bytes_fn=_canned(revised),
                    lake_dir=tmp_path)
    after = (tmp_path / "BRENT.csv").read_text()
    assert out["rows_added"] == 1
    assert after.startswith(before)               # nothing rewritten
    assert after.endswith("2026-07-17,86.0\n")
    assert "99.99" not in after


def test_unknown_series_is_refused(tmp_path):
    try:
        ML.ingest("GOLD", api_key=KEY, fetch_bytes_fn=_canned(),
                  lake_dir=tmp_path)
    except ValueError as e:
        assert "GOLD" in str(e)
    else:
        raise AssertionError("unknown series must not be ingested")


def test_one_dead_series_still_ingests_the_others_and_is_named(tmp_path):
    def flaky(url):
        if "DEXINUS" in url:                      # USDINR is down
            raise ConnectionError("HTTP Error 503")
        return _payload(DAYS)

    slept = []
    out = ML.ingest_all(api_key=KEY, fetch_bytes_fn=flaky,
                        sleep_fn=slept.append, lake_dir=tmp_path,
                        log_path=tmp_path / "o.jsonl")
    assert sorted(out["ok"]) == ["BRENT", "DXY", "US10Y"]
    assert [f["series"] for f in out["failed"]] == ["USDINR"]
    assert "503" in out["failed"][0]["detail"]    # named, not just counted
    assert out["failed"][0]["reason"] == "fetch_failed"
    assert out["rows_added"] == {"BRENT": 3, "DXY": 3, "USDINR": 0,
                                 "US10Y": 3}
    assert not (tmp_path / "USDINR.csv").exists()
    assert len(slept) == 3 and all(2.0 <= s <= 4.0 for s in slept)


def test_dry_run_writes_nothing(tmp_path):
    out = ML.ingest_all(api_key=KEY, fetch_bytes_fn=_canned(),
                        sleep_fn=lambda s: None, lake_dir=tmp_path,
                        dry_run=True)
    assert out["rows_added"] == {"BRENT": 3, "DXY": 3, "USDINR": 3,
                                 "US10Y": 3}
    assert list(tmp_path.iterdir()) == []         # would-append, wrote none
