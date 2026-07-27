"""
tests/test_opportunity_cost.py — Directive 1 (docs/opportunity_cost_design.md).

The load-bearing tests here are the ISOLATION ones: an opportunity-cost row
records what a risk gate REFUSED. It is bookkeeping about our own rules, never
evidence that a pattern works and never training data. Corpus contamination is
silent and permanent, so the guarantee is tested at all three layers.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map, opportunity_cost as oc
from src.discovery import shadow_runner
from src.validation import stat_gates as sg
from src.validation import trial


def _conn():
    return brain_map.connect(":memory:")


def _resolve_host(conn, ref, result="win", r=1.5, day="2026-07-27"):
    """A real trade resolves — the ONLY thing that resolves a shadow."""
    conn.execute(
        "INSERT INTO outcomes (journal_ref, date, ticker, r_multiple, result) "
        "VALUES (?, ?, 'NIFTY', ?, ?)", (ref, day, r, result))
    conn.commit()


# ---------------------------------------------------------------- routing

def test_block_is_recorded_host_linked_and_idempotent():
    conn = _conn()
    first = trial.record_block(conn, gate="exposure_gate",
                               fire_date="2026-07-27", ticker="NIFTY",
                               direction="bullish", host_ref="AB12")
    assert first["created"] and first["ref"].startswith("blocked:")
    again = trial.record_block(conn, gate="exposure_gate",
                               fire_date="2026-07-27", ticker="NIFTY",
                               direction="bullish", host_ref="AB12")
    assert not again["created"] and again["ref"] == first["ref"]
    row = conn.execute("SELECT mode, host_ref, pattern_id FROM shadow_trades "
                       "WHERE journal_ref = ?", (first["ref"],)).fetchone()
    assert row["mode"] == trial.BLOCKED_MODE
    assert row["host_ref"] == "AB12"
    assert row["pattern_id"] == "blocked:exposure_gate"


def test_existing_resolver_resolves_a_blocked_row_with_no_new_code():
    """THE reuse claim: blocked rows are host-linked, so the untouched
    Sleep-Phase Task I sweep resolves them from the blocking position's
    outcome — no second resolver, no parallel price math."""
    conn = _conn()
    res = trial.record_block(conn, gate="exposure_gate",
                             fire_date="2026-07-27", ticker="NIFTY",
                             direction="bullish", host_ref="HOST1")
    assert shadow_runner.resolve_from_outcomes(conn) == 0   # host still open
    _resolve_host(conn, "HOST1", result="loss", r=-1.0)
    assert shadow_runner.resolve_from_outcomes(conn) == 1
    row = conn.execute("SELECT resolved, result, r_multiple FROM "
                       "shadow_trades WHERE journal_ref = ?",
                       (res["ref"],)).fetchone()
    assert row["resolved"] == 1 and row["result"] == "loss"
    assert row["r_multiple"] == -1.0


def test_a_hostless_block_is_never_recorded_as_a_ghost():
    """Halt/margin/veto blocks have no host. The gate seam refuses to
    write a row that could never resolve (and must never be given a
    fabricated outcome)."""
    from src import exposure_gate
    calls = []
    exposure_gate._record_opportunity_cost(
        "NIFTY", "bullish", [{"trade_id": None}],
        record_fn=lambda **kw: calls.append(kw))
    assert calls == []


def test_gate_seam_records_the_blocking_position_as_host():
    from src import exposure_gate
    calls = []
    exposure_gate._record_opportunity_cost(
        "NIFTY", "bearish", [{"trade_id": "XY99"}, {"trade_id": "ZZ11"}],
        record_fn=lambda **kw: calls.append(kw))
    assert len(calls) == 1
    assert calls[0]["host_ref"] == "XY99"      # the first conflicting position
    assert calls[0]["gate"] == "exposure_gate"
    assert calls[0]["ticker"] == "NIFTY" and calls[0]["direction"] == "bearish"


def test_the_suite_can_never_write_into_the_real_brain_map(monkeypatch):
    """REGRESSION (2026-07-27, found within the hour this seam shipped):
    the un-injected path opened the REAL brain_map.db from inside pytest
    and wrote 4 fixture rows, which `python3 -m src.opportunity_cost` then
    reported as '4 duplicate trade(s) refused' — a fabricated number in a
    risk report. Under pytest this seam must be muzzled, exactly like the
    notifier's webhooks, so a future test that forgets a fixture cannot
    re-poison the live record."""
    from src import exposure_gate
    opened = []
    monkeypatch.setattr("src.brain_map.connect",
                        lambda *a, **k: opened.append(a) or (_ for _ in ()).throw(
                            AssertionError("real brain_map.connect() called "
                                           "from inside the test suite")))
    # no record_fn -> would take the live path if the muzzle were missing
    exposure_gate._record_opportunity_cost(
        "NIFTY", "bullish", [{"trade_id": "LIVE1"}])
    assert opened == []


def test_bookkeeping_failure_never_changes_the_verdict():
    """The gate's hard rule: a broken ledger write cannot flip a block."""
    from src import exposure_gate

    def boom(**kwargs):
        raise RuntimeError("db on fire")
    exposure_gate._record_opportunity_cost(
        "NIFTY", "bullish", [{"trade_id": "A1"}], record_fn=boom)  # no raise


# -------------------------------------------------- isolation (3 layers)

def test_layer1_blocked_refs_are_excluded_from_the_learning_corpus():
    ref = trial.block_ref("exposure_gate", "2026-07-27", "NIFTY", "bullish")
    assert not sg.is_learnable_ref(ref)
    assert "blocked:" in sg.EXCLUDED_REF_PREFIXES
    kept = trial.learning_corpus_filter([ref, "AB12", "shadow:xyz", "CD34"])
    assert kept == ["AB12", "CD34"]           # real trades only


def test_layer2_namespaced_pattern_id_cannot_match_a_real_pattern():
    conn = _conn()
    trial.record_block(conn, gate="exposure_gate", fire_date="2026-07-27",
                       ticker="NIFTY", direction="bullish", host_ref="H1")
    _resolve_host(conn, "H1", result="win", r=2.0)
    shadow_runner.resolve_from_outcomes(conn)
    # A real pattern's evidence query sees NOTHING from the blocked row.
    assert trial.shadow_evidence(conn, "pattern_abc") == {"n": 0, "wins": 0}


def test_layer3_mode_filter_holds_even_if_a_real_pattern_id_is_used():
    """Belt, braces and a third belt: if a future caller ever wrote a
    blocked row under a REAL pattern_id, the explicit mode filter must
    still keep it out of that pattern's evidence."""
    conn = _conn()
    trial.ensure_schema(conn)
    conn.execute(
        "INSERT INTO shadow_trades (journal_ref, pattern_id, fire_date, "
        "ticker, resolved, result, created_at, mode) VALUES "
        "('blocked:oops', 'pattern_abc', '2026-07-27', 'NIFTY', 1, 'win', "
        "'2026-07-27T00:00:00', ?)", (trial.BLOCKED_MODE,))
    # a GENUINE shadow for the same pattern, which must still count
    conn.execute(
        "INSERT INTO shadow_trades (journal_ref, pattern_id, fire_date, "
        "ticker, resolved, result, created_at) VALUES "
        "('shadow:real', 'pattern_abc', '2026-07-27', 'NIFTY', 1, 'win', "
        "'2026-07-27T00:00:00')")
    conn.commit()
    ev = trial.shadow_evidence(conn, "pattern_abc")
    assert ev == {"n": 1, "wins": 1}           # the blocked row is invisible


def test_genuine_pattern_shadows_are_unaffected_by_the_new_column():
    """Regression: legacy pattern shadows (mode NULL) still resolve and
    still count as evidence exactly as before."""
    conn = _conn()
    res = trial.record_shadow_fire(conn, "pattern_xyz", "2026-07-27", "TCS")
    conn.execute("UPDATE shadow_trades SET host_ref = 'H9' "
                 "WHERE journal_ref = ?", (res["ref"],))
    conn.commit()
    _resolve_host(conn, "H9", result="win", r=1.2)
    assert shadow_runner.resolve_from_outcomes(conn) == 1
    assert trial.shadow_evidence(conn, "pattern_xyz") == {"n": 1, "wins": 1}


# ------------------------------------------------------------- the report

def test_report_abstains_until_there_is_enough_to_say():
    conn = _conn()
    for i in range(2):
        trial.record_block(conn, gate="exposure_gate",
                           fire_date=f"2026-07-2{i}", ticker="NIFTY",
                           direction="bullish", host_ref=f"H{i}")
        _resolve_host(conn, f"H{i}", result="win", r=1.0, day=f"2026-07-2{i}")
    shadow_runner.resolve_from_outcomes(conn)
    stats = oc.collect(conn=conn)
    assert stats["resolved"] == 2 and stats["verdict"] == "ACCUMULATING"
    assert "Too few resolved" in " ".join(oc.render_lines(stats))


def test_report_says_costing_when_the_blocked_exposure_kept_winning():
    conn = _conn()
    for i in range(6):
        trial.record_block(conn, gate="exposure_gate",
                           fire_date=f"2026-07-1{i}", ticker="NIFTY",
                           direction="bullish", host_ref=f"W{i}")
        _resolve_host(conn, f"W{i}", result="win", r=1.5,
                      day=f"2026-07-1{i}")
    shadow_runner.resolve_from_outcomes(conn)
    stats = oc.collect(conn=conn)
    assert stats["verdict"] == "COSTING" and stats["wins"] == 6
    text = " ".join(oc.render_lines(stats))
    assert "costing" in text
    assert "PROXY" in text                     # honesty caveat is carried
    assert "concentration" in text             # and what it does NOT measure


def test_report_says_saving_when_the_blocked_exposure_kept_losing():
    conn = _conn()
    for i in range(6):
        trial.record_block(conn, gate="exposure_gate",
                           fire_date=f"2026-07-1{i}", ticker="BANKNIFTY",
                           direction="bearish", host_ref=f"L{i}")
        _resolve_host(conn, f"L{i}", result="loss", r=-1.0,
                      day=f"2026-07-1{i}")
    shadow_runner.resolve_from_outcomes(conn)
    stats = oc.collect(conn=conn)
    assert stats["verdict"] == "SAVING" and stats["losses"] == 6
    assert "saving" in " ".join(oc.render_lines(stats))


def test_report_is_honest_when_nothing_has_been_blocked():
    assert "no trades have been blocked yet" in \
        " ".join(oc.render_lines(oc.collect(conn=_conn())))
