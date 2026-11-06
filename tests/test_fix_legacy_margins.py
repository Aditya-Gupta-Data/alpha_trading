"""scripts/fix_legacy_margins.py — pre-#145 debit-spread lock repair. Hermetic: tmp_path DB + journal."""
import importlib.util
import json
import sqlite3
from pathlib import Path

from src import portfolio_manager as pm

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fix_legacy_margins", ROOT / "scripts" / "fix_legacy_margins.py")
fix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fix)

BEAR_PUT = [{"side": "BUY", "option_type": "PE", "strike": 22500.0, "premium": 497.9},
            {"side": "SELL", "option_type": "PE", "strike": 16500.0, "premium": 23.55}]
FLY = [{"side": "BUY", "option_type": "PE", "strike": 900.0, "premium": 5.0},
       {"side": "SELL", "option_type": "PE", "strike": 1000.0, "premium": 30.0},
       {"side": "SELL", "option_type": "CE", "strike": 1000.0, "premium": 30.0},
       {"side": "BUY", "option_type": "CE", "strike": 1100.0, "premium": 5.0}]


def _entry(ref, legs, lot, margin):
    return {"short_id": ref, "spread": {"legs": legs, "lot_size": lot, "margin": {"total_margin": margin}}}


def _world(tmp_path):
    db = tmp_path / "brain_map.db"
    c = sqlite3.connect(str(db))
    pm.ensure_schema(c)
    pm.ensure_accounts_schema(c)
    pm.ensure_buckets_schema(c)
    legacy = (6000 + 474.35) * 65                    # 420,832.75 — the 4e40647d shape
    fly = fix.calculate_span_margin(FLY, 100)["total_margin"]
    fixed = 474.35 * 65                              # a post-#145 debit lock
    c.execute("INSERT INTO margin_locks VALUES ('old1', ?, '2026-10-12T09:19:11', NULL, NULL, 'PAPER_10L/IDX_SPREADS')",
              (legacy,))
    c.execute("INSERT INTO margin_locks VALUES ('fly1', ?, '2026-10-15T09:00:00', NULL, NULL, NULL)", (fly,))
    c.execute("INSERT INTO margin_locks VALUES ('new1', ?, '2026-10-21T09:00:00', NULL, NULL, NULL)", (fixed,))
    c.execute("INSERT INTO margin_locks VALUES ('done1', ?, '2026-10-01T09:00:00', '2026-10-05', 10.0, NULL)",
              (legacy,))
    c.execute("INSERT INTO paper_margin_locks (account_id, journal_ref, margin_rs, lots, primary_lots, locked_at) "
              "VALUES ('PAPER_2L_ROT', 'old1', ?, 1, 1, '2026-10-12T09:19:11')", (legacy * 1.15,))
    c.execute("INSERT INTO portfolio_margin_locks (portfolio_id, journal_ref, margin_rs, lots, locked_at) "
              "VALUES ('PAPER_10L/IDX_SPREADS', 'old1', ?, 1, '2026-10-12T09:19:11')", (legacy,))
    c.commit()
    c.close()
    journal = tmp_path / "journal.jsonl"
    journal.write_text("\n".join(json.dumps(e) for e in [
        _entry("old1", BEAR_PUT, 65, legacy), _entry("fly1", FLY, 100, fly),
        _entry("new1", BEAR_PUT, 65, fixed), _entry("done1", BEAR_PUT, 65, legacy)]) + "\nnot json\n")
    return db, journal


def _locks(db):
    c = sqlite3.connect(str(db))
    try:
        return {"10l": dict(c.execute("SELECT journal_ref, margin_rs FROM margin_locks")),
                "paper": dict(c.execute("SELECT journal_ref, margin_rs FROM paper_margin_locks")),
                "bucket": dict(c.execute("SELECT journal_ref, margin_rs FROM portfolio_margin_locks")),
                "ev": c.execute("SELECT event_type, detail FROM account_events").fetchall(),
                "pev": c.execute("SELECT account_id, event_type FROM paper_account_events").fetchall()}
    finally:
        c.close()


def test_pre_145_formula_matches_the_live_legacy_lock():
    assert fix.pre_145_margin(BEAR_PUT, 65) == round((6000 + 497.9 - 23.55) * 65, 2)
    assert fix.calculate_span_margin(BEAR_PUT, 65)["total_margin"] == round((497.9 - 23.55) * 65, 2)


def test_dry_run_plans_only_open_legacy_debit_locks_and_writes_nothing(tmp_path, capsys):
    db, journal = _world(tmp_path)
    before = _locks(db)
    assert fix.main(["--db", str(db), "--journal", str(journal)]) == 0
    assert _locks(db) == before
    out = capsys.readouterr().out
    assert "3 legacy lock row(s)" in out and "PAPER_10L frees Rs.390,000.00" in out
    assert not list(tmp_path.glob("*.bak-*"))


def test_apply_shrinks_to_the_debit_keeps_stress_and_logs(tmp_path):
    db, journal = _world(tmp_path)
    before = _locks(db)
    archive = tmp_path / "repairs.jsonl"
    assert fix.main(["--db", str(db), "--journal", str(journal), "--archive", str(archive), "--apply"]) == 0
    after = _locks(db)
    assert after["10l"]["old1"] == 30832.75                       # = the trade's max loss
    assert after["paper"]["old1"] == round(30832.75 * 1.15, 2)    # entry-time VIX stress kept
    assert after["bucket"]["old1"] == 30832.75
    for ref in ("fly1", "new1", "done1"):                         # credit, post-fix, released: untouched
        assert after["10l"][ref] == before["10l"][ref]
    assert [e[0] for e in after["ev"]] == [fix.EVENT]
    assert json.loads(after["ev"][0][1])["freed_rs"] == 390000.0
    assert after["pev"] == [("PAPER_2L_ROT", fix.EVENT)]
    assert len(archive.read_text().splitlines()) == 3
    assert len(list(tmp_path.glob("brain_map.db.bak-legacy-margins-*"))) == 1
    assert fix.main(["--db", str(db), "--journal", str(journal), "--archive", str(archive), "--apply"]) == 0
    assert _locks(db)["10l"] == after["10l"]                      # idempotent: nothing left to repair


def test_a_lock_that_moved_since_the_plan_aborts_every_row(tmp_path):
    db, journal = _world(tmp_path)
    c = sqlite3.connect(str(db), isolation_level=None)
    rows = fix.plan(c, fix._read_journal(journal))
    c.execute("UPDATE paper_margin_locks SET margin_rs = 1.0")
    try:
        fix.apply(c, rows, tmp_path / "a.jsonl", "2026-11-06T12:00:00")
        raise AssertionError("expected abort")
    except RuntimeError as e:
        assert "changed since the plan" in str(e)
    assert c.execute("SELECT margin_rs FROM margin_locks WHERE journal_ref = 'old1'").fetchone()[0] == 420832.75
    assert not (tmp_path / "a.jsonl").exists()
    c.close()
