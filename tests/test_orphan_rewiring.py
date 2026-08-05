"""
Sequence 2 — hanging orphans sanitized and re-wired (2026-08-05).

Three data pipelines that were writing into a void, plus one that was
growing without bound:

  * `data/lake/events/` (2,601 partitions, corporate announcements) had NO
    reader. Now it is the evidence for a HARD equity entry halt.
  * `data/lake/intraday_15m.jsonl` was the only NON-partitioned dataset in
    the lake — 25,782 rows / 2.8 MB in three weeks, unbounded, unreadable
    by `lake.read_day`.
  * `data/lake/darlings_daily.jsonl` (105 closes/day, built 08-04 so entry
    zones would be visible) had no consumer. Now it drives the morning
    brief's proximity alerts.

Hermetic: tmp lakes, injected clocks/paths, no network, no real data/ read.
"""
import gzip
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analysis import equity_entry_checks as EEC
from src.ingestion import intraday_tracker as IT
from src import morning_brief as MB

FRESH = {"state": "fresh", "reason": "events lake fresh"}
STALE = {"state": "stale", "reason": "data/lake/events is 20.0 days old"}


def _events_lake(tmp_path, rows_by_day: dict) -> Path:
    """A real gz-JSONL events lake in the layout `lake.scan` expects."""
    root = tmp_path / "lake"
    for day, rows in rows_by_day.items():
        part = root / "events" / f"date={day}"
        part.mkdir(parents=True, exist_ok=True)
        with gzip.open(part / "part.jsonl.gz", "wt") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    return root


def _event(symbol, flags, as_of, subject="Order received"):
    return {"as_of": as_of, "symbol": symbol, "ticker": f"{symbol}.NS",
            "subject": subject, "flags": flags, "attachment": None}


def _long(symbol, instrument="delivery"):
    return {"symbol": symbol, "direction": "long", "instrument": instrument}


# ============================================ 1. corporate risk halt

def test_a_LEGAL_RISK_event_strictly_halts_the_entry(tmp_path):
    """THE test the brief asked for: inject a mocked LEGAL_RISK event and
    prove the entry is blocked."""
    root = _events_lake(tmp_path, {"2026-08-01": [
        _event("TATAMOTORS", ["LEGAL_RISK"], "2026-08-01",
               "SEBI order — penalty imposed")]})
    ok, reason = EEC.corporate_risk_halt(
        _long("TATAMOTORS"), today=date(2026, 8, 5), lake_root=root,
        staleness_fn=lambda name, as_of=None: FRESH)
    assert ok is False
    assert "LEGAL_RISK" in reason
    assert "2026-08-01" in reason
    assert "SEBI order" in reason


def test_a_STRUCTURAL_RISK_event_halts_too(tmp_path):
    root = _events_lake(tmp_path, {"2026-08-02": [
        _event("TATAMOTORS", ["STRUCTURAL_RISK"], "2026-08-02",
               "Scheme of arrangement — demerger")]})
    ok, reason = EEC.corporate_risk_halt(
        _long("TATAMOTORS"), today=date(2026, 8, 5), lake_root=root,
        staleness_fn=lambda name, as_of=None: FRESH)
    assert ok is False and "STRUCTURAL_RISK" in reason


def test_the_halt_is_reachable_through_the_composed_stack(tmp_path):
    """A gate nobody composed is not a gate. Prove `check_entry` blocks."""
    root = _events_lake(tmp_path, {"2026-08-01": [
        _event("VEDL", ["LEGAL_RISK"], "2026-08-01", "NCLT insolvency")]})
    verdict = EEC.check_entry(
        _long("VEDL"), today=date(2026, 8, 5), lake_root=root,
        staleness_fn=lambda name, as_of=None: FRESH,
        queue_path=tmp_path / "no_queue.json",
        levels_path=tmp_path / "no_levels.json")
    assert verdict["allowed"] is False
    assert verdict["blocked_by"] == "corporate_risk_halt"


def test_the_halt_sits_before_the_cheaper_checks_in_the_stack():
    names = [c.__name__ for c in EEC.EQUITY_ENTRY_CHECKS]
    assert "corporate_risk_halt" in names
    # never_short_darling stays first (non-negotiable Law 3), risk second
    assert names.index("never_short_darling") == 0
    assert names.index("corporate_risk_halt") == 1


def test_a_CATALYST_event_does_NOT_halt(tmp_path):
    """Only the two RISK classes gate. An order win must not block a buy."""
    root = _events_lake(tmp_path, {"2026-08-01": [
        _event("ARIS", ["CATALYST"], "2026-08-01", "Bagging of orders"),
        _event("ARIS", ["EXPANSION"], "2026-08-01", "Joint venture")]})
    ok, reason = EEC.corporate_risk_halt(
        _long("ARIS"), today=date(2026, 8, 5), lake_root=root,
        staleness_fn=lambda name, as_of=None: FRESH)
    assert ok is True and reason is None


def test_an_event_outside_the_lookback_no_longer_blocks(tmp_path):
    """A demerger from last year is history, not a live risk."""
    root = _events_lake(tmp_path, {"2026-01-05": [
        _event("X", ["LEGAL_RISK"], "2026-01-05")]})
    ok, _ = EEC.corporate_risk_halt(
        _long("X"), today=date(2026, 8, 5), lake_root=root,
        staleness_fn=lambda name, as_of=None: FRESH)
    assert ok is True


def test_the_halt_is_point_in_time_and_cannot_see_the_future(tmp_path):
    """An announcement filed AFTER the decision day must be invisible —
    otherwise a backtest silently gets tomorrow's newspaper."""
    root = _events_lake(tmp_path, {"2026-08-10": [
        _event("Y", ["LEGAL_RISK"], "2026-08-10")]})
    ok, _ = EEC.corporate_risk_halt(
        _long("Y"), today=date(2026, 8, 5), lake_root=root,
        staleness_fn=lambda name, as_of=None: FRESH)
    assert ok is True


def test_another_tickers_risk_event_does_not_block_us(tmp_path):
    root = _events_lake(tmp_path, {"2026-08-01": [
        _event("OTHER", ["LEGAL_RISK"], "2026-08-01")]})
    ok, _ = EEC.corporate_risk_halt(
        _long("TCS"), today=date(2026, 8, 5), lake_root=root,
        staleness_fn=lambda name, as_of=None: FRESH)
    assert ok is True


def test_a_STALE_events_feed_FAILS_CLOSED(tmp_path):
    """The one debatable call, pinned deliberately. A halt that reads a dead
    feed returns 'no risk' for every ticker forever — that is theatre, and
    it is how sector_index_bars fed a live veto for 20 days. Absent evidence
    must not read as a clean bill of health."""
    root = _events_lake(tmp_path, {"2026-08-01": []})
    ok, reason = EEC.corporate_risk_halt(
        _long("TCS"), today=date(2026, 8, 5), lake_root=root,
        staleness_fn=lambda name, as_of=None: STALE)
    assert ok is False
    assert "stale" in reason and "cannot clear" in reason


def test_a_broken_staleness_guard_also_fails_closed(tmp_path):
    def boom(name, as_of=None):
        raise RuntimeError("guard down")
    ok, reason = EEC.corporate_risk_halt(
        _long("TCS"), today=date(2026, 8, 5), lake_root=tmp_path,
        staleness_fn=boom)
    assert ok is False and "unreadable" in reason


def test_short_proposals_are_not_this_halts_business(tmp_path):
    """never_short_darling owns direction; this halt guards LONG entries."""
    root = _events_lake(tmp_path, {"2026-08-01": [
        _event("Z", ["LEGAL_RISK"], "2026-08-01")]})
    proposal = {"symbol": "Z", "direction": "short", "instrument": "delivery"}
    ok, _ = EEC.corporate_risk_halt(proposal, today=date(2026, 8, 5),
                                    lake_root=root,
                                    staleness_fn=lambda n, as_of=None: FRESH)
    assert ok is True


def test_recent_risk_events_returns_newest_first(tmp_path):
    root = _events_lake(tmp_path, {
        "2026-07-20": [_event("A", ["LEGAL_RISK"], "2026-07-20", "old")],
        "2026-08-01": [_event("A", ["STRUCTURAL_RISK"], "2026-08-01", "new")]})
    hits = EEC.recent_risk_events("A", today=date(2026, 8, 5), lake_root=root)
    assert [h["subject"] for h in hits] == ["new", "old"]


def test_the_ticker_suffix_is_tolerated(tmp_path):
    root = _events_lake(tmp_path, {"2026-08-01": [
        _event("TCS", ["LEGAL_RISK"], "2026-08-01")]})
    assert EEC.recent_risk_events("TCS.NS", today=date(2026, 8, 5),
                                  lake_root=root)


def test_an_absent_lake_is_empty_not_an_exception(tmp_path):
    assert EEC.recent_risk_events("TCS", today=date(2026, 8, 5),
                                  lake_root=tmp_path / "nope") == []


# ============================================ 2. intraday rotation

def test_partition_path_uses_the_lakes_own_layout(tmp_path):
    p = IT.partition_path(tmp_path / "intraday_15m.jsonl", "2026-08-05")
    assert p == tmp_path / "intraday_15m" / "date=2026-08-05" / "part.jsonl"


def test_capture_writes_into_the_days_partition_not_a_flat_file(tmp_path):
    base = tmp_path / "intraday_15m.jsonl"
    res = IT.capture(price_fn=lambda t: 100.0, tickers=["AAA.NS"],
                     out_path=base, force=True, sleep_fn=lambda s: None)
    assert not base.exists()                      # the flat file is gone
    written = Path(res["out"])
    assert written.parent.name.startswith("date=")
    assert json.loads(written.read_text().splitlines()[0])["ticker"] == "AAA.NS"


def test_two_days_land_in_two_partitions_and_neither_overwrites(tmp_path):
    base = tmp_path / "intraday_15m.jsonl"
    IT.append_rows(base, [{"ts": "2026-08-04T10:00:00", "v": 1}], "2026-08-04")
    IT.append_rows(base, [{"ts": "2026-08-05T10:00:00", "v": 2}], "2026-08-05")
    IT.append_rows(base, [{"ts": "2026-08-05T10:15:00", "v": 3}], "2026-08-05")
    days = sorted(p.name for p in (tmp_path / "intraday_15m").glob("date=*"))
    assert days == ["date=2026-08-04", "date=2026-08-05"]
    d5 = IT.partition_path(base, "2026-08-05").read_text().splitlines()
    assert [json.loads(x)["v"] for x in d5] == [2, 3]


def test_the_flat_file_migrates_WITHOUT_DATA_LOSS(tmp_path):
    """Every row lands, keyed by its OWN ts — the history keeps its true
    shape instead of collapsing onto the migration day."""
    base = tmp_path / "intraday_15m.jsonl"
    rows = ([{"ts": f"2026-08-03T09:{i:02d}:00", "ticker": "A", "n": i}
             for i in range(10)]
            + [{"ts": f"2026-08-04T09:{i:02d}:00", "ticker": "B", "n": i}
               for i in range(7)])
    base.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    report = IT.migrate_flat_file(base)
    assert report["status"] == "ok"
    assert report["migrated"] == 17 and report["days"] == 2

    back = []
    for part in sorted((tmp_path / "intraday_15m").rglob("part.jsonl")):
        back += [json.loads(x) for x in part.read_text().splitlines()]
    assert len(back) == 17
    assert sorted(json.dumps(r, sort_keys=True) for r in back) == \
        sorted(json.dumps(r, sort_keys=True) for r in rows)
    # each row is in the partition its own ts names
    d3 = IT.partition_path(base, "2026-08-03").read_text().splitlines()
    assert len(d3) == 10 and all(json.loads(x)["ticker"] == "A" for x in d3)


def test_migration_preserves_the_original_rather_than_deleting_it(tmp_path):
    base = tmp_path / "intraday_15m.jsonl"
    base.write_text(json.dumps({"ts": "2026-08-03T09:00:00"}) + "\n")
    IT.migrate_flat_file(base)
    assert not base.exists()
    assert base.with_suffix(".jsonl.migrated").exists()


def test_migration_is_idempotent_and_a_second_run_is_a_no_op(tmp_path):
    base = tmp_path / "intraday_15m.jsonl"
    base.write_text(json.dumps({"ts": "2026-08-03T09:00:00"}) + "\n")
    IT.migrate_flat_file(base)
    again = IT.migrate_flat_file(base)
    assert again["status"] == "no_flat_file" and again["migrated"] == 0
    assert len(IT.partition_path(base, "2026-08-03")
               .read_text().splitlines()) == 1


def test_an_undated_or_corrupt_row_is_parked_never_dropped(tmp_path):
    base = tmp_path / "intraday_15m.jsonl"
    base.write_text("\n".join([
        json.dumps({"ts": "2026-08-03T09:00:00", "n": 1}),
        json.dumps({"no_ts": True, "n": 2}),
        "{not json at all",
    ]) + "\n")
    report = IT.migrate_flat_file(base)
    assert report["migrated"] == 3 and report["undated"] == 2
    unknown = IT.partition_path(base, "unknown").read_text().splitlines()
    assert len(unknown) == 2


def test_the_darling_day_tap_partitions_too(tmp_path):
    base = tmp_path / "darlings_daily.jsonl"
    res = IT.capture_darlings(price_fn=lambda t: 10.0, tickers=["AAA"],
                              out_path=base, force=True,
                              sleep_fn=lambda s: None)
    assert Path(res["out"]).parent.name.startswith("date=")


# ============================================ 3. buy-zone proximity

LEVELS = {
    "INZONE":  {"symbol": "INZONE",  "buy_zone": [100.0, 110.0]},
    "NEAR":    {"symbol": "NEAR",    "buy_zone": [100.0, 110.0]},
    "FAR":     {"symbol": "FAR",     "buy_zone": [100.0, 110.0]},
    "BELOW":   {"symbol": "BELOW",   "buy_zone": [100.0, 110.0]},
    "NOZONE":  {"symbol": "NOZONE"},
}


def test_a_darling_inside_its_zone_is_reported_as_actionable():
    hits = MB.buy_zone_proximity({"INZONE": 105.0}, LEVELS)
    assert len(hits) == 1
    assert hits[0]["state"] == "IN ZONE" and hits[0]["distance_pct"] == 0.0


def test_a_darling_just_above_its_zone_is_APPROACHING():
    hits = MB.buy_zone_proximity({"NEAR": 112.0}, LEVELS)      # +1.8%
    assert hits[0]["state"] == "APPROACHING"
    assert hits[0]["distance_pct"] == 1.82


def test_a_darling_far_above_its_zone_is_not_reported():
    assert MB.buy_zone_proximity({"FAR": 130.0}, LEVELS) == []


def test_a_close_BELOW_the_zone_is_not_an_entry_alert():
    """Below the floor is the stop/overextension side of the book, which
    equity_entry_checks owns — surfacing it here would read as 'buy'."""
    assert MB.buy_zone_proximity({"BELOW": 80.0}, LEVELS) == []


def test_a_name_without_levels_is_skipped_never_assumed():
    assert MB.buy_zone_proximity({"NOZONE": 100.0, "UNKNOWN": 5.0},
                                 LEVELS) == []


def test_in_zone_names_sort_ahead_of_approaching_ones():
    hits = MB.buy_zone_proximity(
        {"NEAR": 112.0, "INZONE": 105.0}, LEVELS)
    assert [h["symbol"] for h in hits] == ["INZONE", "NEAR"]


def test_junk_prices_and_zones_never_raise():
    assert MB.buy_zone_proximity({"INZONE": None}, LEVELS) == []
    assert MB.buy_zone_proximity({"INZONE": "abc"}, LEVELS) == []
    assert MB.buy_zone_proximity({"A": 1.0}, {"A": {"buy_zone": [0, 0]}}) == []
    assert MB.buy_zone_proximity({}, {}) == []
    assert MB.buy_zone_proximity(None, None) == []


def test_the_day_tap_reader_takes_only_the_NEWEST_day(tmp_path):
    p = tmp_path / "darlings_daily.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in [
        {"day": "2026-08-03", "ticker": "A", "price": 1.0},
        {"day": "2026-08-04", "ticker": "A", "price": 2.0},
        {"day": "2026-08-04", "ticker": "B.NS", "price": 3.0},
    ]) + "\n")
    assert MB._latest_darling_closes(p) == {"A": 2.0, "B": 3.0}


def test_the_day_tap_reader_also_reads_the_partitioned_layout(tmp_path):
    base = tmp_path / "darlings_daily.jsonl"
    part = tmp_path / "darlings_daily" / "date=2026-08-05"
    part.mkdir(parents=True)
    (part / "part.jsonl").write_text(
        json.dumps({"day": "2026-08-05", "ticker": "A", "price": 9.0}) + "\n")
    assert MB._latest_darling_closes(base) == {"A": 9.0}


def test_the_card_field_renders_both_states_and_the_advisory_caveat():
    text = MB._proximity_field(closes={"INZONE": 105.0, "NEAR": 112.0},
                               levels=LEVELS)
    assert "INZONE" in text and "in zone" in text
    assert "NEAR" in text and "above" in text
    assert "Advisory only" in text


def test_a_quiet_morning_adds_NO_field_at_all():
    """No hits => None => the card stays byte-identical to before."""
    assert MB._proximity_field(closes={"FAR": 130.0}, levels=LEVELS) is None
    payload = MB.build_morning_brief(darling_closes={"FAR": 130.0},
                                     darling_levels=LEVELS,
                                     journal_path="/nonexistent.jsonl",
                                     calendar={})
    assert not any("Proximity" in f["name"] for f in payload["fields"])


def test_the_proximity_field_reaches_the_actual_card():
    payload = MB.build_morning_brief(darling_closes={"INZONE": 105.0},
                                     darling_levels=LEVELS,
                                     journal_path="/nonexistent.jsonl",
                                     calendar={})
    field = [f for f in payload["fields"] if "Proximity" in f["name"]]
    assert len(field) == 1
    assert "INZONE" in field[0]["value"]


def test_the_long_list_is_capped_for_discord():
    closes = {f"S{i}": 105.0 for i in range(20)}
    levels = {f"S{i}": {"symbol": f"S{i}", "buy_zone": [100.0, 110.0]}
              for i in range(20)}
    text = MB._proximity_field(closes=closes, levels=levels)
    assert "…and 12 more" in text
