"""
Level 1 upgrade batch (2026-08-05): ATR trailing stops, dual-horizon
sentiment storage, and the cross-asset EOD tap.

The safety property each block pins, stated once:
  * the trail touches ONLY the plan-carrying equity path and is OPT-IN, so
    the 19 resolved options spreads resolve byte-identically;
  * the sentiment columns are ADDITIVE and NULL-honest, so no existing row
    loses a value or gains a fabricated one;
  * the cross-asset clerk cannot crash the equity pipeline, whatever the
    other market's calendar, entitlement or contract expiry is doing.

Hermetic: injected bars/fetchers, tmp DBs and tmp lakes. No network.
"""
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src import plan_tracker as PT
from src.ingestion import cross_asset as CA


# ==================================================== 1. ATR trailing stop

def _bar(day, low, high, close):
    return (day, low, high, close)


def _rising(n=30, start=100.0, step=2.0, spread=1.0):
    """A clean uptrend: each bar `step` higher, `spread` wide."""
    out = []
    for i in range(n):
        base = start + i * step
        out.append(_bar(f"2026-01-{i + 1:02d}", base - spread,
                        base + spread, base))
    return out


def _plan(stop, target, trailing=None):
    plan = {"stop_loss": {"price": stop}, "target": {"price": target}}
    if trailing is not None:
        plan["trailing"] = trailing
    return plan


def _entry(plan, entry_day="2026-01-01"):
    return {"date": entry_day, "decision": "approved", "ticker": "T.NS",
            "outcome": None, "plan": plan}


def test_without_a_trailing_spec_resolution_is_byte_identical():
    """THE regression guard. Every existing journal entry lacks `trailing`,
    so the old bracket behaviour must be reproduced exactly."""
    bars = _rising(20)
    plain = PT._resolve(_entry(_plan(90.0, 120.0)), bars)
    assert plain == ("target_hit", 120.0, "2026-01-11")


def test_atr_is_none_on_a_history_too_short_to_measure():
    """An ATR from 3 bars is a guess, and a guess must not move a stop."""
    assert PT.atr_from_bars(_rising(5), n=14) is None
    assert PT.atr_from_bars([], n=14) is None
    assert PT.atr_from_bars(_rising(20), n=14) is not None


def test_atr_matches_the_true_range_definition():
    bars = [_bar("d1", 10.0, 12.0, 11.0), _bar("d2", 11.0, 14.0, 13.0),
            _bar("d3", 12.0, 15.0, 14.0)]
    # TR(d2) = max(14,11)-min(11,11) = 3 ; TR(d3) = max(15,13)-min(12,13) = 3
    assert PT.atr_from_bars(bars, n=2) == 3.0


def test_a_null_bar_never_produces_a_fabricated_atr():
    bars = [_bar("d1", 10.0, 12.0, None), _bar("d2", 11.0, 14.0, 13.0)]
    assert PT.atr_from_bars(bars, n=1) is None


def test_the_trail_ratchets_up_and_banks_a_reversal():
    """Price runs, the floor follows, price collapses -> trail_hit well
    above the original stop. The bracket would have ridden it to 90."""
    # 20 rising bars peak at high=139, ATR=3.0 => floor 139-2*3 = 133.
    # The reversal bar dips to 120: below the trail, ABOVE the hard stop.
    bars = _rising(20) + [_bar("2026-02-01", 120.0, 140.0, 121.0)]
    entry = _entry(_plan(90.0, 999.0, {"atr_mult": 2.0}))
    res, price, day = PT._resolve(entry, bars)
    assert res == "trail_hit"
    assert day == "2026-02-01"
    assert price == 133.0          # the ratcheted floor, not the 90 stop
    # Without the trail the same bars never produce a stop or target exit
    # at all — the position just ages out at the last close, giving back
    # everything above 121. That difference IS the upgrade.
    plain = PT._resolve(_entry(_plan(90.0, 999.0)), bars)
    assert plain[0] == "time_stop" and plain[1] == 121.0


def test_the_trail_can_never_sit_below_the_plans_own_stop():
    """Risk-reducing by construction: the floor is clamped at the stop, so
    a trail can only ever improve on the bracket, never worsen it."""
    bars = _rising(20, start=100.0, step=0.1, spread=8.0)   # wide, flat
    entry = _entry(_plan(99.0, 999.0, {"atr_mult": 5.0}))
    out = PT._resolve(entry, bars)
    if out is not None:
        assert out[1] >= 99.0


def test_the_hard_stop_still_wins_on_the_same_bar():
    """The module's standing pessimistic same-bar rule is untouched."""
    bars = _rising(20) + [_bar("2026-02-01", 50.0, 140.0, 55.0)]
    entry = _entry(_plan(95.0, 999.0, {"atr_mult": 1.0}))
    res, price, _ = PT._resolve(entry, bars)
    assert res == "stop_hit" and price == 95.0


def test_the_target_still_closes_the_trade():
    bars = _rising(30)
    entry = _entry(_plan(90.0, 120.0, {"atr_mult": 2.0}))
    res, price, _ = PT._resolve(entry, bars)
    assert res == "target_hit" and price == 120.0


def test_the_floor_is_never_raised_using_the_bar_it_is_tested_against():
    """Ratchet-after-check. Otherwise a single wide bar both lifts the
    floor and trips it — an exit that could not happen in real trading."""
    bars = _rising(20) + [_bar("2026-02-01", 100.0, 200.0, 105.0)]
    entry = _entry(_plan(90.0, 999.0, {"atr_mult": 0.1}))
    res, price, _ = PT._resolve(entry, bars)
    assert res == "trail_hit"
    # The floor used is the one standing BEFORE this bar (139 - 0.1*3 =
    # 138.7). Had the ratchet run first it would have used this bar's own
    # 200 high (~199.7) and manufactured an exit that cannot happen.
    assert price == 138.7


def test_a_malformed_or_disabled_trail_spec_is_simply_off():
    assert PT.trailing_config({"plan": {}}) is None
    assert PT.trailing_config({"plan": {"trailing": "yes"}}) is None
    assert PT.trailing_config({"plan": {"trailing": {"atr_mult": 0}}}) is None
    assert PT.trailing_config({"plan": {"trailing": {"atr_mult": -1}}}) is None
    bad = PT.trailing_config({"plan": {"trailing": {"atr_mult": "abc"}}})
    assert bad["atr_mult"] == PT.TRAIL_ATR_MULT_DEFAULT


def test_a_trail_exit_is_scored_on_the_realised_move_not_assumed_a_win():
    """A trail that ratchets and is then hit BELOW entry is still a loss.
    Calling it a win would flatter the record."""
    e = {"decision": "approved"}
    assert "WIN" in PT._verdict(e, "trail_hit", 10.0)
    assert "LOSS" in PT._verdict(e, "trail_hit", -10.0)
    assert "flat" in PT._verdict(e, "trail_hit", 0.0)


def test_spreads_are_untouched_by_the_trail():
    """The 19 resolved options trades live in _resolve_spread, which has no
    trail and needs none — a vertical is defined-risk, its max loss capped
    by construction. An anti-drift lock on that separation."""
    import inspect
    src = inspect.getsource(PT._resolve_spread)
    assert "trail" not in src.lower()
    assert PT.OPTION_PROFIT_TAKE_FRACTION == 0.65     # untouched


# ============================================ 2. dual-horizon sentiment

def test_the_migration_adds_both_columns_without_dropping_a_row(tmp_path):
    db = tmp_path / "b.db"
    conn = brain_map.connect(str(db))
    brain_map.record_event(conn, "2026-08-01", "TCS.NS", "news", "legacy")
    conn.close()

    conn = brain_map.connect(str(db))              # re-open = re-migrate
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
    assert {"short_term_catalyst_score", "long_term_macro_score"} <= cols
    rows = conn.execute("SELECT tag, short_term_catalyst_score, "
                        "long_term_macro_score FROM events").fetchall()
    assert len(rows) == 1 and rows[0]["tag"] == "legacy"
    # NULL-honest: a pre-feature row is unknown, NOT a fabricated neutral 0
    assert rows[0]["short_term_catalyst_score"] is None
    assert rows[0]["long_term_macro_score"] is None
    conn.close()


def test_the_migration_is_idempotent(tmp_path):
    db = tmp_path / "b.db"
    for _ in range(3):
        conn = brain_map.connect(str(db))
        conn.close()
    conn = brain_map.connect(str(db))
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(events)")]
    assert cols.count("short_term_catalyst_score") == 1
    conn.close()


def test_ingest_stores_BOTH_horizons(tmp_path):
    conn = brain_map.connect(str(tmp_path / "b.db"))
    brain_map.ingest_existing(conn, journal_entries=[], news={
        "generated": "2026-08-05",
        "tickers": {"TCS.NS": {"sentiment_score": 3,
                               "short_term_catalyst_score": 3,
                               "long_term_macro_score": -2,
                               "headline_focus": "order win"}}})
    row = conn.execute(
        "SELECT short_term_catalyst_score, long_term_macro_score, entities "
        "FROM events WHERE event_type = 'news'").fetchone()
    assert row["short_term_catalyst_score"] == 3.0
    assert row["long_term_macro_score"] == -2.0
    assert json.loads(row["entities"])["long_term_macro_score"] == -2
    conn.close()


def test_an_absent_long_horizon_stays_NULL_never_zero(tmp_path):
    """'We do not know' and 'genuinely neutral' are different readings and
    the miners must be able to tell them apart."""
    conn = brain_map.connect(str(tmp_path / "b.db"))
    brain_map.ingest_existing(conn, journal_entries=[], news={
        "generated": "2026-08-05",
        "tickers": {"X.NS": {"sentiment_score": 1, "headline_focus": "f"}}})
    row = conn.execute("SELECT short_term_catalyst_score, "
                       "long_term_macro_score FROM events").fetchone()
    assert row["short_term_catalyst_score"] == 1.0
    assert row["long_term_macro_score"] is None
    conn.close()


def test_a_re_ingest_REFRESHES_the_scores_on_the_same_event(tmp_path):
    """_get_or_create_event dedupes and returns the existing id, so the
    stamp has to be a separate UPDATE or a corrected score would be lost."""
    conn = brain_map.connect(str(tmp_path / "b.db"))
    news = {"generated": "2026-08-05",
            "tickers": {"X.NS": {"sentiment_score": 1,
                                 "short_term_catalyst_score": 1,
                                 "long_term_macro_score": 1,
                                 "headline_focus": "f"}}}
    brain_map.ingest_existing(conn, journal_entries=[], news=news)
    news["tickers"]["X.NS"]["long_term_macro_score"] = -4
    brain_map.ingest_existing(conn, journal_entries=[], news=news)
    rows = conn.execute("SELECT long_term_macro_score FROM events").fetchall()
    assert len(rows) == 1 and rows[0]["long_term_macro_score"] == -4.0
    conn.close()


def test_horizon_divergence_finds_the_disagreements_only(tmp_path):
    """The reason the second dimension is worth storing: +4 short on -3
    long is a different trade from +4 on +3."""
    conn = brain_map.connect(str(tmp_path / "b.db"))
    brain_map.ingest_existing(conn, journal_entries=[], news={
        "generated": "2026-08-05",
        "tickers": {
            "SPLIT.NS": {"sentiment_score": 4, "short_term_catalyst_score": 4,
                         "long_term_macro_score": -3, "headline_focus": "a"},
            "ALIGNED.NS": {"sentiment_score": 4, "short_term_catalyst_score": 4,
                           "long_term_macro_score": 3, "headline_focus": "b"},
            "UNKNOWN.NS": {"sentiment_score": 4, "headline_focus": "c"}}})
    hits = brain_map.horizon_divergence(conn)
    assert [h["ticker"] for h in hits] == ["SPLIT.NS"]
    conn.close()


def test_set_event_horizons_never_raises_on_a_bad_id(tmp_path):
    conn = brain_map.connect(str(tmp_path / "b.db"))
    assert brain_map.set_event_horizons(conn, None, 1, 1) is False
    assert brain_map.set_event_horizons(conn, 99999, 1, 1) is True   # no-op
    conn.close()


# ================================================ 3. cross-asset tap

INSTR = {"CRUDE": {"id": "1", "seg": "MCX_COMM", "inst": "FUTCOM",
                   "asset_class": "commodity", "_expiry": "2026-12-31"}}


def _payload(days, base=100.0):
    epoch = int(datetime(2026, 8, 1).timestamp())
    return {"timestamp": [epoch + i * 86400 for i in range(days)],
            "open": [base + i for i in range(days)],
            "high": [base + i + 2 for i in range(days)],
            "low": [base + i - 2 for i in range(days)],
            "close": [base + i + 1 for i in range(days)],
            "volume": [1000 + i for i in range(days)]}


def test_a_good_fetch_lands_date_partitioned_lake_rows(tmp_path):
    rep = CA.run(instruments=INSTR, fetch_fn=lambda i, s, e: _payload(3),
                 today=date(2026, 8, 5), lake_root=tmp_path,
                 ledger_path=tmp_path / "l.jsonl")
    assert rep["rows_written"] == 3 and rep["days_written"] == 3
    from src import lake
    rows = lake.read_day("cross_asset", "2026-08-01", root=tmp_path)
    assert rows and rows[0]["name"] == "CRUDE"
    assert rows[0]["asset_class"] == "commodity"


def test_a_holiday_or_unentitled_segment_is_a_NAMED_SKIP_not_a_crash(tmp_path):
    """MCX, NSE and the global indices share no calendar. The clerk refuses
    to reason about 'is the market open' — an empty window is honest."""
    rep = CA.run(instruments=INSTR, fetch_fn=lambda i, s, e: None,
                 today=date(2026, 8, 5), lake_root=tmp_path,
                 ledger_path=tmp_path / "l.jsonl")
    assert rep["ok"] == [] and rep["rows_written"] == 0
    assert rep["skipped"][0]["code"] == "CA-404"
    assert (tmp_path / "l.jsonl").exists()


def test_an_exploding_fetch_cannot_crash_the_pass(tmp_path):
    def boom(i, s, e):
        raise RuntimeError("segment not entitled")
    rep = CA.run(instruments=INSTR, fetch_fn=boom, today=date(2026, 8, 5),
                 lake_root=tmp_path, ledger_path=tmp_path / "l.jsonl")
    assert rep["skipped"][0]["code"] == "CA-500"
    assert "not entitled" in rep["skipped"][0]["detail"]


def test_one_dead_instrument_never_costs_the_others(tmp_path):
    instr = dict(INSTR)
    instr["GOLD_INDIA"] = {"id": "2", "seg": "MCX_COMM", "inst": "FUTCOM",
                           "asset_class": "commodity"}

    def selective(i, s, e):
        return None if i["id"] == "1" else _payload(2)
    rep = CA.run(instruments=instr, fetch_fn=selective, today=date(2026, 8, 5),
                 lake_root=tmp_path, ledger_path=tmp_path / "l.jsonl")
    assert [r["name"] for r in rep["ok"]] == ["GOLD_INDIA"]
    assert rep["rows_written"] == 2


def test_an_expired_contract_is_named_as_expired_not_as_no_data(tmp_path):
    """An expired MCX id does not error — it silently returns nothing, and
    'no bars' without 'because the contract died' sends you looking in the
    wrong place. GOLD_INDIA's real id expires 2026-08-05."""
    instr = {"GOLD": {"id": "9", "seg": "MCX_COMM", "inst": "FUTCOM",
                      "asset_class": "commodity", "_expiry": "2026-08-04"}}
    rep = CA.run(instruments=instr, fetch_fn=lambda i, s, e: _payload(3),
                 today=date(2026, 8, 5), lake_root=tmp_path,
                 ledger_path=tmp_path / "l.jsonl")
    assert rep["expired"] == ["GOLD"]
    assert rep["skipped"][0]["code"] == "CA-410"
    assert "expired" in rep["skipped"][0]["detail"]
    assert rep["rows_written"] == 0          # never priced a dead contract


def test_a_partial_bar_is_dropped_never_zero_filled():
    p = _payload(3)
    p["close"][1] = None
    rows = CA.bars_from_payload(p, "CRUDE", "commodity")
    assert len(rows) == 2
    assert all(r["close"] is not None for r in rows)


def test_ragged_arrays_never_index_past_the_end():
    p = _payload(5)
    p["high"] = p["high"][:2]
    assert len(CA.bars_from_payload(p, "X", "commodity")) == 2


def test_junk_payloads_are_empty_not_an_exception():
    assert CA.bars_from_payload(None, "X", "c") == []
    assert CA.bars_from_payload({}, "X", "c") == []
    assert CA.bars_from_payload({"timestamp": ["bad"], "open": [1],
                                 "high": [1], "low": [1], "close": [1]},
                                "X", "c") == []


def test_dry_run_writes_no_lake_rows(tmp_path):
    rep = CA.run(instruments=INSTR, fetch_fn=lambda i, s, e: _payload(2),
                 today=date(2026, 8, 5), lake_root=tmp_path,
                 ledger_path=tmp_path / "l.jsonl", dry_run=True)
    assert rep["rows_written"] == 0
    assert not (tmp_path / "cross_asset").exists()


def test_instruments_reuse_the_ONE_verified_id_file(tmp_path):
    """A second copy of an instrument id is a second thing to let rot
    (ledger Issues 14/15). The commodity legs come from the same
    macro_securities.json the macro tracker already uses."""
    sec = tmp_path / "sec.json"
    sec.write_text(json.dumps({
        "_note": "docs, not an instrument",
        "CRUDE": {"id": "560977", "seg": "MCX_COMM", "inst": "FUTCOM"},
        "GOLD_INDIA": {"id": "466583", "seg": "MCX_COMM", "inst": "FUTCOM"},
        "USDINR": {"id": "", "seg": "", "inst": ""}}))
    got = CA.load_instruments(securities_path=sec,
                              global_path=tmp_path / "absent.json")
    assert set(got) == {"CRUDE", "GOLD_INDIA"}       # incomplete entry skipped
    assert all(v["asset_class"] == "commodity" for v in got.values())


def test_an_incomplete_global_entry_is_skipped_never_guessed(tmp_path):
    g = tmp_path / "g.json"
    g.write_text(json.dumps({
        "_status": "docs",
        "GOOD": {"id": "5", "seg": "X", "inst": "Y"},
        "HALF": {"id": "6", "seg": "X"}}))
    got = CA.load_instruments(securities_path=tmp_path / "absent.json",
                              global_path=g)
    assert set(got) == {"GOOD"}
    assert got["GOOD"]["asset_class"] == "global_index"


def test_the_shipped_global_config_is_empty_by_design_and_says_so():
    """It ships EMPTY because no id was verified against the scrip master,
    and the project rule forbids writing an unverified one."""
    cfg = json.loads((Path(__file__).resolve().parent.parent
                      / "config" / "global_indices.json").read_text())
    instruments = {k: v for k, v in cfg.items() if not k.startswith("_")}
    assert instruments == {}
    assert "EMPTY BY DESIGN" in cfg["_status"]


def test_the_module_is_imported_by_nothing_on_the_trading_path():
    """Capture-only. If this ever gains a Dept 2/3 importer it stops being
    a safe expansion and becomes a live dependency."""
    import subprocess
    root = Path(__file__).resolve().parent.parent
    hits = subprocess.run(
        ["grep", "-rl", "--include=*.py", "cross_asset", str(root / "src")],
        capture_output=True, text=True).stdout.split()
    assert [Path(h).name for h in hits] == ["cross_asset.py"]
