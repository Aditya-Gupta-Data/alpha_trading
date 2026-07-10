"""
Offline verification suite for the Phase 7 scratchpad build:

  * src/ingestion/macro_tracker.py — trend classification, the Dhan live
    path (mocked payloads, including the doubly-nested shape and a DH-906
    token death), the snapshot fail-open, and the index-impact mapping.
  * src/ingestion/news_parser.py — strict five-key coercion, entity
    canonicalization, and the Ollama fail-safes (all HTTP mocked).
  * src/knowledge_graph/resonance.py — the CONFLICT / RESONANCE / NEUTRAL
    verdicts against a simulated live book (the spec scenario: a NIFTY
    bear put spread vs crude crashing short-term while structurally
    spiking long-term), advisory strike/expiry adjustment payloads, and
    the read-only graph guarantees.

Fully offline: no broker socket, no Ollama instance, no reads of the real
journal/brain_map and no writes outside temp dirs.

Run either of these from the project folder:
    python tests/test_resonance.py        (simple, no extra installs)
    python -m pytest tests/               (if you have pytest)
"""

import json
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map, local_parser
from src.dhan_guard import SafeDhanClient
from src.ingestion import macro_tracker, news_parser
from src.ingestion.macro_tracker import (build_macro_matrix, classify_trend,
                                         coerce_direction)
from src.ingestion.news_parser import canonicalize_entity, parse_headline
from src.knowledge_graph import resonance
from src.knowledge_graph.resonance import (evaluate_portfolio_resonance,
                                           log_advisories)

TODAY = date(2026, 7, 10)

MISSING = "/nonexistent/phase7"   # a path that never resolves to a file


# --------------------------------------------------------------- fixtures

def _spread_entry(short_id, ticker, expiry, strategy="bear_put_spread",
                  strikes=(24050.0, 23850.0)):
    """One open journal spread entry shaped exactly like the live book's
    (see data/journal.jsonl's bear put spreads from 2026-07-09)."""
    return {
        "short_id": short_id, "ticker": ticker, "decision": "approved",
        "outcome": None, "date": "2026-07-09",
        "signal": f"bearish trend read on {ticker} — {strategy}",
        "spread": {
            "strategy": strategy, "direction": "bearish",
            "legs": [
                {"side": "BUY", "option_type": "PE",
                 "strike": strikes[0], "premium": 207.75},
                {"side": "SELL", "option_type": "PE",
                 "strike": strikes[1], "premium": 132.70},
            ],
            "expiry": expiry, "lots": 1, "net_debit": 75.05,
            "max_loss": 75.05, "max_profit": 124.95,
        },
    }


def _equity_entry(short_id, ticker="TCS", price=3600.0):
    return {"short_id": short_id, "ticker": ticker, "decision": "approved",
            "outcome": None, "date": "2026-07-08",
            "signal": "Fresh Golden Cross", "price": price,
            "plan": {"variant": "swing", "stop_loss": 3490.0,
                     "target": 3820.0}}


def _write_json(dirpath, name, payload) -> str:
    path = Path(dirpath) / name
    path.write_text(json.dumps(payload))
    return str(path)


# The spec scenario's matrix inputs: crude CRASHING short-term but
# structurally SPIKING long-term, rupee strengthening short-term (USDINR
# falling) but structurally weakening, gold soft near-term. Short-term
# the tape turns bullish for the indexes; long-term it stays bearish.
CONFLICT_SNAPSHOT = {"as_of": "2026-07-10", "metrics": {
    "CRUDE":      {"short_term": "crashing", "medium_term": "flat",
                   "long_term": "spiking", "level": 5100.0},
    "USDINR":     {"short_term": "falling", "medium_term": "flat",
                   "long_term": "rising", "level": 86.4},
    "GOLD_WORLD": {"short_term": "falling", "medium_term": "flat",
                   "long_term": "flat"},
    "GOLD_INDIA": {"short_term": "falling", "medium_term": "flat",
                   "long_term": "flat"},
}}

# Everything inflationary rising across horizons: bearish tape for the
# indexes — what a bear put spread wants to see.
RESONANCE_SNAPSHOT = {"as_of": "2026-07-10", "metrics": {
    "CRUDE":      {"short_term": "rising", "medium_term": "rising",
                   "long_term": "rising"},
    "USDINR":     {"short_term": "rising", "medium_term": "rising",
                   "long_term": "flat"},
    "GOLD_WORLD": {"short_term": "flat", "medium_term": "flat",
                   "long_term": "flat"},
    "GOLD_INDIA": {"short_term": "flat", "medium_term": "flat",
                   "long_term": "flat"},
}}

CRUDE_CRASH_EVENT = {"target_entity": "CRUDE",
                     "event_classification": "supply_shock",
                     "directional_bias": -0.9, "horizon_impact": "SHORT",
                     "confidence_score": 0.8}

NIFTY_BEAR_EVENT = {"target_entity": "NIFTY",
                    "event_classification": "macro_liquidity",
                    "directional_bias": -0.8, "horizon_impact": "SHORT",
                    "confidence_score": 0.9}


def _matrix_from(snapshot: dict) -> dict:
    """Build a real matrix through the module's own snapshot loader (no
    live path: the securities file is pointed at nothing)."""
    with tempfile.TemporaryDirectory() as tmp:
        snap = _write_json(tmp, "snap.json", snapshot)
        return build_macro_matrix(snapshot_path=snap,
                                  securities_path=MISSING, today=TODAY)


# ===================================================== macro_tracker: trends

def test_classify_trend_rising_falling_flat_unknown():
    rising = [100.0, 100.0, 100.0, 101.0, 102.0, 102.0, 103.0]
    falling = list(reversed(rising))
    assert classify_trend(rising, 5, 0.75) == "rising"
    assert classify_trend(falling, 5, 0.75) == "falling"
    assert classify_trend([100.0] * 7, 5, 0.75) == "flat"
    assert classify_trend([100.0, 101.0], 5, 0.75) == "unknown"   # too short
    assert classify_trend(None, 5, 0.75) == "unknown"


def test_coerce_direction_accepts_human_and_llm_spellings():
    assert coerce_direction("Crashing") == "falling"
    assert coerce_direction("SPIKING") == "rising"
    assert coerce_direction("range-bound") == "flat"
    assert coerce_direction("appreciating") == "rising"
    assert coerce_direction("meh") == "unknown"
    assert coerce_direction(None) == "unknown"


# ============================================ macro_tracker: snapshot fallback

def test_snapshot_fallback_builds_full_matrix_offline():
    matrix = _matrix_from(CONFLICT_SNAPSHOT)
    crude = matrix["metrics"]["CRUDE"]
    assert crude["horizons"] == {"SHORT": "falling", "MEDIUM": "flat",
                                 "LONG": "rising"}
    assert crude["level"] == 5100.0
    assert crude["source"] == "snapshot"
    assert matrix["source"] == "snapshot"
    # All four metrics falling short-term -> raw NIFTY impact 1.2, clamped:
    assert matrix["index_impact"]["NIFTY 50"]["SHORT"] == 1.0
    assert matrix["index_impact"]["NIFTY 50"]["MEDIUM"] == 0.0
    # Long-term: crude + USDINR structurally rising -> bearish index read.
    assert matrix["index_impact"]["NIFTY 50"]["LONG"] == -0.9
    assert matrix["index_impact"]["NIFTY BANK"]["SHORT"] == 1.0


def test_missing_everything_degrades_to_unknown_never_raises():
    matrix = build_macro_matrix(snapshot_path=MISSING,
                                securities_path=MISSING, today=TODAY)
    assert matrix["source"] == "none"
    for m in matrix["metrics"].values():
        assert set(m["horizons"].values()) == {"unknown"}
    for row in matrix["index_impact"].values():
        assert set(row.values()) == {0.0}
    assert matrix["gold_divergence"] is False


def test_gold_india_world_divergence_flag():
    snap = {"metrics": {
        "GOLD_INDIA": {"short_term": "rising"},
        "GOLD_WORLD": {"short_term": "falling"},
    }}
    assert _matrix_from(snap)["gold_divergence"] is True
    assert _matrix_from(CONFLICT_SNAPSHOT)["gold_divergence"] is False


# ================================================ macro_tracker: Dhan live path

def _fake_dhan_client(response):
    client = mock.Mock()
    client.historical_daily_data.return_value = response
    return client


def test_dhan_live_path_computes_horizons_from_bars():
    closes = [100.0 + i * 0.5 for i in range(140)]   # steady structural rise
    # The doubly-nested payload shape dhan_guard exists to unwrap:
    response = {"status": "success",
                "data": {"data": {"timestamp": list(range(140)),
                                  "close": closes}}}
    with tempfile.TemporaryDirectory() as tmp:
        sec = _write_json(tmp, "sec.json", {
            "CRUDE": {"id": "429", "seg": "MCX_COMM", "inst": "FUTCOM"}})
        fake = _fake_dhan_client(response)
        with mock.patch("src.dhan_client._get_client", return_value=fake):
            matrix = build_macro_matrix(snapshot_path=MISSING,
                                        securities_path=sec, today=TODAY)
    crude = matrix["metrics"]["CRUDE"]
    assert crude["source"] == "dhan"
    assert crude["horizons"] == {"SHORT": "rising", "MEDIUM": "rising",
                                 "LONG": "rising"}
    assert crude["level"] == closes[-1]
    # The call went out with the verified MCX identifiers from the file:
    args = fake.historical_daily_data.call_args.args
    assert args[:3] == ("429", "MCX_COMM", "FUTCOM")
    # Unmapped metrics never hit the API:
    assert fake.historical_daily_data.call_count == 1
    assert matrix["metrics"]["USDINR"]["source"] == "none"


def test_dhan_auth_failure_fails_open_to_snapshot_with_audit():
    dead_token = {"status": "failure",
                  "remarks": {"error_code": "DH-906",
                              "error_message": "Invalid Token"}}
    with tempfile.TemporaryDirectory() as tmp:
        sec = _write_json(tmp, "sec.json", {
            "CRUDE": {"id": "429", "seg": "MCX_COMM", "inst": "FUTCOM"}})
        snap = _write_json(tmp, "snap.json", CONFLICT_SNAPSHOT)
        safe = SafeDhanClient()
        with mock.patch("src.dhan_client._get_client",
                        return_value=_fake_dhan_client(dead_token)), \
             mock.patch("src.dhan_guard.time.sleep"):   # skip retry pause
            matrix = build_macro_matrix(snapshot_path=snap,
                                        securities_path=sec, safe=safe,
                                        today=TODAY)
    # Fail-open: the snapshot answered anyway...
    assert matrix["metrics"]["CRUDE"]["horizons"]["SHORT"] == "falling"
    assert matrix["metrics"]["CRUDE"]["source"] == "snapshot"
    # ...and the failure is classified and audited, not swallowed:
    assert safe.auth_failures()
    assert safe.last_error.code == "DH-906"


def test_dhan_offline_no_credentials_fails_open():
    with tempfile.TemporaryDirectory() as tmp:
        sec = _write_json(tmp, "sec.json", {
            "USDINR": {"id": "1", "seg": "NSE_CURRENCY", "inst": "FUTCUR"}})
        snap = _write_json(tmp, "snap.json", CONFLICT_SNAPSHOT)
        with mock.patch("src.dhan_client._get_client", return_value=None):
            matrix = build_macro_matrix(snapshot_path=snap,
                                        securities_path=sec, today=TODAY)
    assert matrix["metrics"]["USDINR"]["horizons"]["LONG"] == "rising"
    assert matrix["metrics"]["USDINR"]["source"] == "snapshot"


# ======================================================= news_parser: parsing

def _mock_ollama(content: str, status_code: int = 200):
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    return resp


def test_parse_headline_returns_strict_five_key_frame():
    raw = {"target_entity": "NIFTY", "event_classification": "Macro Liquidity",
           "directional_bias": -0.8, "horizon_impact": "short_term",
           "confidence_score": 0.9}
    with mock.patch("httpx.post",
                    return_value=_mock_ollama(json.dumps(raw))) as post:
        frame = parse_headline("RBI drains liquidity via 7-day VRRR auction")
    assert frame == {"target_entity": "NIFTY 50",
                     "event_classification": "macro_liquidity",
                     "directional_bias": -0.8,
                     "horizon_impact": "SHORT",
                     "confidence_score": 0.9}
    # Local endpoint only, strict schema in the system prompt:
    assert "11434" in post.call_args.args[0]
    prompt = post.call_args.kwargs["json"]["messages"][0]["content"]
    for key in ("target_entity", "event_classification", "directional_bias",
                "horizon_impact", "confidence_score"):
        assert key in prompt


def test_parse_headline_coerces_fences_words_and_ranges():
    raw = ("```json\n" + json.dumps({
        "target_entity": "Brent", "event_classification": "supply shock!!",
        "directional_bias": "bullish", "horizon_impact": "structural",
        "confidence_score": "85"}) + "\n```")
    with mock.patch("httpx.post", return_value=_mock_ollama(raw)):
        frame = parse_headline("OPEC extends production cuts through 2027")
    assert frame["target_entity"] == "CRUDE"          # alias -> canonical
    assert frame["event_classification"] == "supply_shock"
    assert frame["directional_bias"] == 0.5           # word -> number
    assert frame["horizon_impact"] == "LONG"          # structural -> LONG
    assert frame["confidence_score"] == 0.85          # percent -> fraction


def test_parse_headline_clamps_out_of_range_numbers():
    raw = {"target_entity": "USD/INR", "event_classification": "fx",
           "directional_bias": 5, "horizon_impact": "weeks",
           "confidence_score": -3}
    with mock.patch("httpx.post",
                    return_value=_mock_ollama(json.dumps(raw))):
        frame = parse_headline("Rupee at record low")
    assert frame["target_entity"] == "USDINR"
    assert frame["directional_bias"] == 1.0
    assert frame["confidence_score"] == 0.0
    assert frame["horizon_impact"] == "MEDIUM"


def test_parse_headline_fails_safe_on_junk_and_missing_entity():
    with mock.patch("httpx.post", return_value=_mock_ollama("not json at all")):
        assert parse_headline("gibberish") is None
    no_entity = {"event_classification": "chat", "directional_bias": 0,
                 "horizon_impact": "SHORT", "confidence_score": 0}
    with mock.patch("httpx.post",
                    return_value=_mock_ollama(json.dumps(no_entity))):
        assert parse_headline("nothing tradable here") is None
    assert parse_headline("") is None
    assert parse_headline(None) is None


def test_parse_headline_ollama_offline_returns_none_quietly():
    local_parser._OLLAMA_OFFLINE_REPORTED = False
    try:
        with mock.patch("httpx.post",
                        side_effect=Exception("[Errno 111] Connection refused")):
            assert parse_headline("Crude spikes 4% on OPEC cut") is None
    finally:
        local_parser._OLLAMA_OFFLINE_REPORTED = False


def test_canonicalize_entity_shared_vocabulary():
    assert canonicalize_entity("nifty") == "NIFTY 50"
    assert canonicalize_entity("Bank Nifty") == "NIFTY BANK"
    assert canonicalize_entity("^NSEI") == "NIFTY 50"
    assert canonicalize_entity("TCS.NS") == "TCS"
    assert canonicalize_entity("WTI") == "CRUDE"
    assert canonicalize_entity("gold") == "GOLD_WORLD"
    assert canonicalize_entity("MCX Gold") == "GOLD_INDIA"
    assert canonicalize_entity("rupee") == "USDINR"
    assert canonicalize_entity("  ") is None
    assert canonicalize_entity(None) is None


# ============================================ resonance: the spec scenario

def test_bear_put_spread_conflict_with_crude_crash_and_expiry_roll_advice():
    """The mandated scenario: a live NIFTY bear put spread (expiring in 6
    days) against a complex matrix where crude is crashing short-term but
    structurally spiking long-term. Short-term the tape turns bullish for
    NIFTY — the bearish spread is fighting it -> CONFLICT + cut-loss
    advisory; and because the LONG horizon still favors the bearish
    thesis, the advisory carries the roll-to-further-expiry adjustment."""
    entries = [
        _spread_entry("25da25ec", "NIFTY 50", "2026-07-16"),
        _spread_entry("7b84bd44", "NIFTY BANK", "2026-07-16",
                      strikes=(57300.0, 56900.0)),
    ]
    matrix = _matrix_from(CONFLICT_SNAPSHOT)
    payloads = evaluate_portfolio_resonance(CRUDE_CRASH_EVENT, matrix,
                                            entries=entries, today=TODAY,
                                            db_path=MISSING)
    assert len(payloads) == 2
    by_ticker = {p["ticker"]: p for p in payloads}
    nifty = by_ticker["NIFTY 50"]

    assert nifty["verdict"] == "CONFLICT"
    assert abs(nifty["alignment"] - (-0.51)) < 1e-6
    assert nifty["days_to_expiry"] == 6
    # Short horizon fully against the trade, long horizon still with it:
    assert nifty["horizon_scores"]["SHORT"] == 1.0
    assert nifty["horizon_scores"]["LONG"] == -0.9
    # The risk-mitigation payload: exit advisory + expiry-roll adjustment.
    assert nifty["actions"] == ["EXIT_ADVISORY", "ROLL_EXPIRY_ADVISORY"]
    assert nifty["suggested_adjustment"]["type"] == "roll_expiry"
    assert nifty["suggested_adjustment"]["current_expiry"] == "2026-07-16"
    assert "exit / cut-loss" in nifty["advisory"]
    assert "further expiry" in nifty["advisory"]

    assert by_ticker["NIFTY BANK"]["verdict"] == "CONFLICT"


def test_far_dated_spread_same_matrix_is_not_a_conflict():
    """Same tape, but a spread expiring ~2.5 months out lives on the
    MEDIUM/LONG read — the structural bearish leg offsets the near-term
    squeeze and the verdict stays NEUTRAL. The horizon matrix, not just
    the sign, decides."""
    entries = [_spread_entry("deadbe12", "NIFTY 50", "2026-09-24")]
    matrix = _matrix_from(CONFLICT_SNAPSHOT)
    (payload,) = evaluate_portfolio_resonance(CRUDE_CRASH_EVENT, matrix,
                                              entries=entries, today=TODAY,
                                              db_path=MISSING)
    assert payload["verdict"] == "NEUTRAL"
    assert payload["days_to_expiry"] == 76
    assert payload["blend_weights"]["LONG"] == 0.40


def test_bear_put_spread_resonance_suggests_strike_roll():
    entries = [_spread_entry("25da25ec", "NIFTY 50", "2026-07-16")]
    matrix = _matrix_from(RESONANCE_SNAPSHOT)
    (payload,) = evaluate_portfolio_resonance(NIFTY_BEAR_EVENT, matrix,
                                              entries=entries, today=TODAY,
                                              db_path=MISSING)
    assert payload["verdict"] == "RESONANCE"
    assert payload["alignment"] >= resonance.RESONANCE_THRESHOLD
    assert payload["actions"] == ["EXTEND_TARGET_ADVISORY",
                                  "ROLL_STRIKE_ADVISORY"]
    adj = payload["suggested_adjustment"]
    assert adj["type"] == "roll_strikes"
    # One spread-width WITH the bearish move: 24050/23850 -> 23850/23650.
    assert adj["from_strikes"] == [24050.0, 23850.0]
    assert adj["to_strikes"] == [23850.0, 23650.0]
    assert adj["strike_width"] == 200.0
    # The slower horizons carry the move -> further-expiry hint attaches.
    assert "expiry_hint" in adj
    assert "extending targets" in payload["advisory"]


def test_equity_long_resonates_with_bullish_stock_event():
    entries = [_equity_entry("aa11bb22", ticker="TCS")]
    event = {"target_entity": "TCS.NS",
             "event_classification": "earnings_surprise",
             "directional_bias": 0.9, "horizon_impact": "SHORT",
             "confidence_score": 0.9}
    (payload,) = evaluate_portfolio_resonance(event, None, entries=entries,
                                              today=TODAY, db_path=MISSING)
    assert payload["kind"] == "equity"
    assert payload["direction"] == 1
    assert payload["verdict"] == "RESONANCE"
    assert payload["actions"] == ["EXTEND_TARGET_ADVISORY"]
    assert payload["suggested_adjustment"] is None   # no strikes to roll


def test_equity_long_inherits_damped_index_macro():
    """A single stock rides the NIFTY 50 impact row times the beta damp —
    the short-horizon bullish tape from the conflict snapshot resonates
    with a long swing even with no stock-specific event."""
    entries = [_equity_entry("aa11bb22", ticker="TCS")]
    matrix = _matrix_from(CONFLICT_SNAPSHOT)
    (payload,) = evaluate_portfolio_resonance(None, matrix, entries=entries,
                                              today=TODAY, db_path=MISSING)
    assert payload["horizon_scores"]["SHORT"] == round(
        1.0 * resonance.STOCK_INDEX_BETA, 3)
    assert payload["verdict"] == "RESONANCE"


def test_non_directional_structure_is_always_neutral():
    entries = [_spread_entry("cc33dd44", "NIFTY 50", "2026-07-16",
                             strategy="iron_condor")]
    matrix = _matrix_from(RESONANCE_SNAPSHOT)
    (payload,) = evaluate_portfolio_resonance(NIFTY_BEAR_EVENT, matrix,
                                              entries=entries, today=TODAY,
                                              db_path=MISSING)
    assert payload["verdict"] == "NEUTRAL"
    assert payload["direction"] == 0
    assert payload["actions"] == []
    assert "non-directional" in payload["advisory"]


def test_no_inputs_at_all_is_neutral_and_never_raises():
    entries = [_spread_entry("25da25ec", "NIFTY 50", "2026-07-16")]
    (payload,) = evaluate_portfolio_resonance(None, None, entries=entries,
                                              today=TODAY, db_path=MISSING)
    assert payload["verdict"] == "NEUTRAL"
    assert payload["alignment"] == 0.0
    assert set(payload["horizon_scores"].values()) == {0.0}


def test_closed_and_rejected_entries_are_not_evaluated():
    closed = _spread_entry("ee55ff66", "NIFTY 50", "2026-07-16")
    closed["outcome"] = {"resolution": "target_hit"}
    rejected = _spread_entry("11aa22bb", "NIFTY BANK", "2026-07-16")
    rejected["decision"] = "rejected"
    matrix = _matrix_from(CONFLICT_SNAPSHOT)
    assert evaluate_portfolio_resonance(CRUDE_CRASH_EVENT, matrix,
                                        entries=[closed, rejected],
                                        today=TODAY, db_path=MISSING) == []


def test_injected_entries_never_touch_the_real_journal():
    with mock.patch("src.journal.read_all",
                    side_effect=AssertionError("real journal was read")):
        evaluate_portfolio_resonance(
            None, None, entries=[_equity_entry("aa11bb22")], today=TODAY,
            db_path=MISSING)


# ======================================== resonance: read-only graph guard

def _seed_graph(db_path, tag="macro_liquidity"):
    """A tiny brain_map with two outcomes (one win, one loss) linked to
    events carrying `tag` — built through brain_map's own schema."""
    conn = brain_map.connect(db_path)
    for i, result in enumerate(("win", "loss")):
        event_id = conn.execute(
            "INSERT INTO events (date, ticker, event_type, tag) "
            "VALUES (?, ?, ?, ?)",
            ("2026-07-01", "NIFTY 50", "macro", tag)).lastrowid
        outcome_id = conn.execute(
            "INSERT INTO outcomes (journal_ref, date, ticker, result) "
            "VALUES (?, ?, ?, ?)",
            (f"ref-{i}", "2026-07-02", "NIFTY 50", result)).lastrowid
        conn.execute(
            "INSERT INTO event_outcome_link (event_id, outcome_id) "
            "VALUES (?, ?)", (event_id, outcome_id))
    conn.commit()
    conn.close()


def test_graph_context_enriches_advisory_via_read_only_sqlite():
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "bm.db")
        _seed_graph(db, tag="macro_liquidity")
        entries = [_spread_entry("25da25ec", "NIFTY 50", "2026-07-16")]
        matrix = _matrix_from(RESONANCE_SNAPSHOT)
        (payload,) = evaluate_portfolio_resonance(
            NIFTY_BEAR_EVENT, matrix, entries=entries, today=TODAY,
            db_path=db)
    assert payload["graph_context"] == {"tags": ["bear_put_spread",
                                                 "macro_liquidity"],
                                        "count": 2, "win_rate": 0.5}
    assert "Graph: 2 linked outcome(s)" in payload["advisory"]


def test_read_only_connection_cannot_write():
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "bm.db")
        _seed_graph(db)
        conn = resonance._read_only_connection(db)
        try:
            try:
                conn.execute("INSERT INTO events (date, ticker, event_type, "
                             "tag) VALUES ('x', 'x', 'x', 'x')")
                raised = False
            except sqlite3.OperationalError:
                raised = True
        finally:
            conn.close()
    assert raised, "mode=ro must refuse writes"


def test_graph_context_absent_db_degrades_to_none():
    entries = [_equity_entry("aa11bb22")]
    (payload,) = evaluate_portfolio_resonance(
        None, None, entries=entries, today=TODAY,
        db_path="/nonexistent/dir/bm.db")
    assert payload["graph_context"] is None


# ============================================================== advisory log

def test_log_advisories_appends_plain_jsonl():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "logs" / "resonance_advisories.jsonl"
        entries = [_spread_entry("25da25ec", "NIFTY 50", "2026-07-16")]
        matrix = _matrix_from(CONFLICT_SNAPSHOT)
        payloads = evaluate_portfolio_resonance(CRUDE_CRASH_EVENT, matrix,
                                                entries=entries, today=TODAY,
                                                db_path=MISSING)
        log_advisories(payloads, path=path)
        log_advisories(payloads, path=path)   # append, not overwrite
        lines = [json.loads(l) for l in path.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["verdict"] == "CONFLICT"
    assert lines[0]["trade_id"] == "25da25ec"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}  {e}")
    print(f"\n{passed}/{len(tests)} tests passed.")
