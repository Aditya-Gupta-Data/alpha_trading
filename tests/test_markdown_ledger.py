"""
tests/test_markdown_ledger.py — the LIVE TRADE BOOK render (2026-09-19).

Hermetic: a tmp journal, a tmp equity event stream and a tmp brain_map.db
built from portfolio_manager's own schema. Pins the honesty rules —
`—` not 0 for the unrecorded, telemetry shadows never tabled, hypothetical
outcomes and rejected proposals excluded, ledger lock preferred over the
journal's own margin figure, and a read-only, never-raising render.
"""
import json
import sqlite3
from pathlib import Path

import pytest

from src import portfolio_manager as pm
from src.reporting import markdown_ledger as ml

TODAY = "2026-09-19"


def _spread(short_id, ticker, created, outcome=None, decision="approved",
            strategy="bear_put_spread", margin=16341.0):
    return {"short_id": short_id, "ticker": ticker, "decision": decision,
            "created_at": f"{created}T13:00:37+05:30", "date": created,
            "spread": {"strategy": strategy, "max_loss": 8650.0,
                       "margin": {"total_margin": margin}},
            "outcome": outcome}


def _outcome(pnl, r, exit_date, verdict="WIN — auto-exit", hypothetical=False):
    return {"pnl_rs": pnl, "r_multiple": r, "exit_date": exit_date,
            "verdict": verdict, "hypothetical": hypothetical,
            "resolution": "profit_take"}


def _entry(ev_id, ticker, ts, funded=True, qty=8, price=100.0):
    return {"event": "entry", "id": ev_id, "ticker": ticker,
            "mode": "PAPER_CAPITAL" if funded else "PAPER_TELEMETRY",
            "ts": f"{ts}T19:15:27+05:30", "as_of": ts,
            "kyu_trigger": {"setup": "darling_buy"},
            "kya_kara_action": {"entry_price": price, "qty": qty},
            "funding": {"funded": funded, "qty": qty if funded else 0,
                        "notional": qty * price if funded else 0,
                        "lock_ref": f"eqd:{ev_id}"}}


def _exit(ev_id, ts, price, category="WIN", r=1.2, reason="target"):
    return {"event": "exit", "id": ev_id, "ts": f"{ts}T15:30:00+05:30",
            "exit_price": price, "reason": reason,
            "kya_sikha_autopsy": {"category": category, "r_multiple": r}}


@pytest.fixture
def book(tmp_path):
    journal = tmp_path / "journal.jsonl"
    equity = tmp_path / "equity.jsonl"
    db = tmp_path / "brain_map.db"

    rows = [
        _spread("open1", "NIFTY 50", "2026-09-16"),
        _spread("open2", "TCS.NS", "2026-09-11", margin=45934.0),  # no lock row
        _spread("res1", "NIFTY BANK", "2026-09-10",
                _outcome(15245.3, 1.81, "2026-09-15")),
        _spread("res2", "ICICIBANK.NS", "2026-08-20",
                _outcome(-9125.0, -0.93, "2026-08-24", "LOSS — closed behind")),
        _spread("hypo", "RELIANCE.NS", "2026-08-01",
                _outcome(999.0, 3.0, "2026-08-05", hypothetical=True)),
        _spread("rej", "HDFCBANK.NS", "2026-09-01", decision="rejected"),
        {"short_id": "eqplan", "decision": "approved", "plan": {}, "outcome": None},
    ]
    journal.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    events = [
        _entry("eq_open", "FINEORG.NS", "2026-07-20", price=4933.0),
        _entry("eq_done", "TCS.NS", "2026-08-01", qty=10, price=3000.0),
        _exit("eq_done", "2026-09-01", 3200.0),
        _entry("tele_open", "WAAREERTL.NS", "2026-08-10", funded=False),
        _entry("tele_done", "MACPOWER.NS", "2026-08-10", funded=False),
        _exit("tele_done", "2026-08-20", 90.0, category="LOSS", r=-1.0),
    ]
    equity.write_text("\n".join(json.dumps(e) for e in events) + "\n")

    conn = sqlite3.connect(db)
    pm.ensure_schema(conn)
    conn.execute("INSERT INTO account_state VALUES (1, 1000000, 95901.49, "
                 "1095901.49, '2026-07-21T09:00:00')")
    conn.execute("INSERT INTO equity_curve VALUES ('2026-09-19T05:33:13', "
                 "1095901.49, 1095901.49, 0.0)")
    conn.execute("INSERT INTO equity_curve VALUES ('2026-09-01T05:33:13', "
                 "1050000, 1090000, 4.073)")
    conn.executemany(
        "INSERT INTO margin_locks VALUES (?,?,?,?,?)",
        [("open1", 34443.5, "2026-09-16T09:17:19", None, None),
         ("res1", 33000.0, "2026-09-10T13:00:37", "2026-09-15T15:30:00", 15245.3),
         ("eqd:eq_open", 39464.0, "2026-07-20T19:15:27", None, None),
         ("eqd:eq_done", 30000.0, "2026-08-01T19:15:27", "2026-09-01T15:30:00", 1950.0)])
    conn.commit()
    conn.close()
    return {"journal": journal, "equity": equity, "db": db}


def _render(book, **kw):
    return ml.render(journal_path=book["journal"], equity_path=book["equity"],
                     db_path=book["db"], today=TODAY, now="test", **kw)


def test_account_line_reads_the_ledger_not_a_recompute(book):
    text = _render(book)
    assert "| Starting capital | ₹1,000,000 |" in text
    assert "| Realized P&L (ledger) | ₹95,901 |" in text
    assert "| Equity | ₹1,095,901 |" in text
    assert "| Drawdown now | -0.00% (at peak) |" in text
    assert "| Max drawdown (curve) | -4.07% |" in text
    # open locks only: open1 + eqd:eq_open
    assert "| Margin locked (open) | ₹73,908 |" in text
    assert "| Open trades | 3 (2 options, 1 equity) |" in text


def test_open_table_prefers_the_ledger_lock_and_flags_the_fallback(book):
    text = _render(book)
    assert "| NIFTY 50 | Options | Bear Put Spread | 2026-09-16 | ₹34,444 | 3 | open1 |" in text
    assert "| TCS.NS | Options | Bear Put Spread | 2026-09-11 | ₹45,934† | 8 | open2 |" in text
    assert "| FINEORG.NS | Equity | Equity Darling Buy | 2026-07-20 | ₹39,464 | 61 | eq_open |" in text
    assert "† no `margin_locks` row" in text


def test_resolved_table_is_newest_first_with_pnl_r_and_verdict(book):
    text = _render(book)
    res = text.split("## RECENTLY RESOLVED")[1]
    i_bank = res.index("| NIFTY BANK |")
    i_tcs = res.index("| TCS.NS | Equity |")
    i_icici = res.index("| ICICIBANK.NS |")
    assert i_bank < i_tcs < i_icici          # 09-15, 09-01, 08-24
    assert "| ₹15,245 | +1.81R | WIN — auto-exit |" in res
    assert "| -₹9,125 | -0.93R | LOSS — closed behind |" in res
    # equity P&L is the desk's settled pnl_net (1950), not qty x diff (2000)
    assert "| ₹1,950 | +1.20R | WIN · Target Hit |" in res
    assert "(last 3 of 3)" in text


def test_hypothetical_rejected_and_telemetry_rows_never_table(book):
    text = _render(book)
    assert "RELIANCE.NS" not in text          # #31 hypothetical outcome
    assert "HDFCBANK.NS" not in text          # rejected proposal
    assert "WAAREERTL.NS" not in text and "MACPOWER.NS" not in text
    assert "Equity telemetry shadows (zero capital, never tabled): 1 open, 1 resolved." in text


def test_resolved_is_capped_at_fifty(book):
    rows = [_spread(f"r{i}", "NIFTY 50", "2026-08-01",
                    _outcome(100.0, 0.1, f"2026-08-{(i % 28) + 1:02d}"))
            for i in range(60)]
    book["journal"].write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    book["equity"].write_text("")
    text = _render(book)
    assert "(last 50 of 60)" in text
    assert text.count("| NIFTY 50 | Options |") == 50


def test_unrecorded_values_render_as_dash_never_zero(book):
    rows = [_spread("nor", "NIFTY 50", "2026-09-01",
                    {"exit_date": "2026-09-05", "verdict": "closed"})]
    book["journal"].write_text(json.dumps(rows[0]) + "\n")
    book["equity"].write_text("")
    text = _render(book)
    assert "| NIFTY 50 | Options | Bear Put Spread | 2026-09-05 | — | — | closed |" in text
    assert "over 0 with a rupee outcome" in text


def test_missing_db_and_files_degrade_to_notes_not_exceptions(tmp_path):
    text = ml.render(journal_path=tmp_path / "nope.jsonl",
                     equity_path=tmp_path / "nope2.jsonl",
                     db_path=tmp_path / "nope.db", today=TODAY, now="test")
    assert "account figures withheld" in text
    assert "_No open positions._" in text
    assert "_No resolved trades yet._" in text


def test_write_is_atomic_and_the_db_is_untouched(book, tmp_path):
    before = book["db"].read_bytes()
    out = ml.write(tmp_path / "docs" / "LIVE_TRADE_BOOK.md",
                   journal_path=book["journal"], equity_path=book["equity"],
                   db_path=book["db"], today=TODAY, now="test")
    assert out.exists() and out.read_text().startswith("# LIVE TRADE BOOK")
    assert not out.with_suffix(".md.tmp").exists()
    assert book["db"].read_bytes() == before


def test_module_imports_nothing_from_the_trading_path():
    """Read-only by construction: no tracker / desk / treasury / journal
    writer can be reached from this module."""
    imports = [l for l in Path(ml.__file__).read_text().splitlines()
               if l.startswith(("import ", "from "))]
    for banned in ("plan_tracker", "equity_desk", "firm_treasury",
                   "journal", "notifier", "portfolio_manager"):
        assert not any(banned in l for l in imports), banned


def test_cron_and_sync_wiring_present():
    root = Path(ml.__file__).resolve().parents[2]
    cron = (root / "scripts" / "setup_cron.sh").read_text()
    assert "35 16 * * 1-5" in cron and "src.reporting.markdown_ledger" in cron
    sync = (root / "scripts" / "mac_auto_sync.sh").read_text()
    assert "vm_pull_file" in sync and "docs/LIVE_TRADE_BOOK.md" in sync
    assert "docs/LIVE_TRADE_BOOK.md" in (root / ".gitignore").read_text()
