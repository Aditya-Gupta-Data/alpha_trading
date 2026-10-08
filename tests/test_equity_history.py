"""The multi-portfolio equity graph (owner request 2026-10-09): the VM's
forward record of TRUE NET EQUITY (src/equity_history.py) and the dashboard's
reader (`data.equity_history`, `window_start`, `in_window`) that turns the
realized curves + that record into % return on contributed capital."""
from datetime import datetime

from src import brain_map, portfolio_manager as pm
from src import equity_history as eh
from src.dashboard import data as d

TUE = datetime(2026, 10, 6, 11, 0)          # a trading day
HOLIDAY = datetime(2026, 10, 2, 11, 0)      # Gandhi Jayanti (nse_calendar)


def _treasury(**over):
    t = {"PAPER_10L": {"equity": 1_100_000.0, "unrealized_pnl": -5_000.0, "net_equity": 1_095_000.0,
                       "starting_capital": 1_000_000.0, "marks_as_of": "2026-10-06T10:59:00"},
         "PAPER_2L_LIVE": {"equity": 200_000.0, "unrealized_pnl": 1_500.0, "net_equity": 201_500.0,
                           "starting_capital": 200_000.0, "marks_as_of": "2026-10-06T10:58:30"},
         "PAPER_2L": {"equity": 210_000.0, "unrealized_pnl": None, "net_equity": None,
                      "starting_capital": 200_000.0, "open_positions": 3},
         "PAPER_2L_ROT": {"equity": 198_000.0, "unrealized_pnl": None, "net_equity": None,
                          "starting_capital": 200_000.0, "open_positions": 0},
         "equity_curve": [], "benchmarks": {}}
    t.update(over)
    return t


def test_record_writes_one_row_per_priced_account_and_names_the_rest(tmp_path):
    c = brain_map.connect(str(tmp_path / "bm.db"))
    try:
        out = eh.record(now=TUE, conn=c, treasury_fn=_treasury)
        assert out["recorded"] == ["PAPER_10L", "PAPER_2L_LIVE", "PAPER_2L_ROT"]
        assert "3 open position(s), not all priced" in out["skipped"]["PAPER_2L"]
        rows = c.execute("SELECT account_id, ts, equity, unrealized_pnl, net_equity, starting_capital, marks_as_of "
                         "FROM net_equity_history ORDER BY account_id").fetchall()
        assert [tuple(r) for r in rows] == [
            ("PAPER_10L", "2026-10-06T11:00:00", 1_100_000.0, -5_000.0, 1_095_000.0, 1_000_000.0, "2026-10-06T10:59:00"),
            ("PAPER_2L_LIVE", "2026-10-06T11:00:00", 200_000.0, 1_500.0, 201_500.0, 200_000.0, "2026-10-06T10:58:30"),
            ("PAPER_2L_ROT", "2026-10-06T11:00:00", 198_000.0, 0.0, 198_000.0, 200_000.0, None)]   # flat: net = realized
        eh.record(now=TUE, conn=c, treasury_fn=_treasury)          # same instant again: no duplicate
        assert c.execute("SELECT COUNT(*) FROM net_equity_history").fetchone()[0] == 3
    finally:
        c.close()


def test_record_abstains_on_a_holiday_a_weekend_and_an_unreadable_treasury(tmp_path):
    c = brain_map.connect(str(tmp_path / "bm.db"))
    try:
        assert "not an NSE trading day" in eh.record(now=HOLIDAY, conn=c, treasury_fn=_treasury)["skipped_day"]
        assert "not an NSE trading day" in eh.record(now=datetime(2026, 10, 10, 11, 0), conn=c,
                                                    treasury_fn=_treasury)["skipped_day"]
        assert "unreadable" in eh.record(now=TUE, conn=c, treasury_fn=lambda: {"error": "locked"})["skipped_day"]
        assert c.execute("SELECT name FROM sqlite_master WHERE name = 'net_equity_history'").fetchone() is None
    finally:
        c.close()


def test_record_reads_the_kpi_cards_own_treasury(tmp_path, monkeypatch):
    """End to end on a real (tmp) book with nothing open: each row is the KPI
    card's realized equity (the page's journal and snapshot pointed at tmp)."""
    monkeypatch.setattr(d, "JOURNAL_PATH", tmp_path / "journal.jsonl")
    monkeypatch.setattr(d, "SNAPSHOT_PATH", tmp_path / "market_snapshot.json")
    p = tmp_path / "bm.db"
    c = brain_map.connect(str(p))
    pm.get_account(c)
    pm.get_paper_account(c, "PAPER_2L")
    c.close()
    T = d.treasury(p)
    c = brain_map.connect(str(p))
    try:
        out = eh.record(now=TUE, conn=c, treasury_fn=lambda: d.treasury(p))
        assert set(out["recorded"]) == {"PAPER_10L", "PAPER_2L"}
        got = dict(c.execute("SELECT account_id, net_equity FROM net_equity_history").fetchall())
        assert got == {a: T[a]["equity"] for a in ("PAPER_10L", "PAPER_2L")}
        assert all(T[a]["open_positions"] == 0 for a in ("PAPER_10L", "PAPER_2L"))
    finally:
        c.close()


def test_the_cli_never_fails_the_mirror_push(monkeypatch, capsys):
    monkeypatch.setattr(eh, "record", lambda: (_ for _ in ()).throw(RuntimeError("disk full")))
    assert eh.main(["--record"]) == 0
    assert "not recorded: disk full" in capsys.readouterr().out


# ----------------------------------------------------------------- the reader
def _book(tmp_path, injection_detail=None):
    p = tmp_path / "bm.db"
    c = brain_map.connect(str(p))
    pm.get_account(c)
    pm.get_paper_account(c, "PAPER_2L")
    eh.ensure_schema(c)
    c.execute("DELETE FROM equity_curve")
    c.executemany("INSERT INTO equity_curve (ts, equity, peak_equity, drawdown_pct) VALUES (?, ?, 0, 0)", [
        ("2026-07-10T11:32:07", 1_000_000.0), ("2026-07-21T05:56:17", 1_026_982.14),
        ("2026-07-21T14:32:30", 1_026_982.14),            # stamped AT the clean sheet: pre-reset equity
        ("2026-07-23T05:45:10", 205_538.96), ("2026-08-04T05:07:11", 239_423.99),
        ("2026-08-07T16:41:19", 1_039_423.99),            # stamped AT the injection: post-injection equity
        ("2026-09-28T11:44:41", 1_127_100.62)])
    c.execute("INSERT INTO account_events (ts, event_type, detail) VALUES ('2026-07-21T14:32:30', 'clean_sheet', "
              "'decision #84: pool reset 10L->2L for the autonomous run (old numbers preserved)')")
    c.execute("INSERT INTO account_events (ts, event_type, detail) VALUES ('2026-08-07T16:41:19', "
              "'capital_injection', ?)", (injection_detail or "Rs.800,000.00 (200,000.00 -> 1,000,000.00 base; "
                                         "equity 239,423.99 -> 1,039,423.99)",))
    c.execute("UPDATE paper_accounts SET created_at = '2026-09-21T12:06:35' WHERE account_id = 'PAPER_2L'")
    c.execute("INSERT INTO paper_equity_curve (account_id, ts, equity, peak_equity, drawdown_pct) "
              "VALUES ('PAPER_2L', '2026-09-25T15:28:58', 206696.18, 0, 0)")
    c.execute("INSERT INTO net_equity_history VALUES ('PAPER_2L', '2026-10-09T09:30:00', 210000, -1000, 209000, "
              "200000, NULL, NULL)")
    c.commit()
    c.close()
    return p


def _pcts(h, acct="PAPER_10L"):
    return {p["ts"]: p["pct"] for p in h["realized"] if p["account"] == acct}


def test_paper_10l_is_measured_on_the_capital_contributed_at_the_time(tmp_path):
    h = d.equity_history(_book(tmp_path))
    got = _pcts(h)
    assert got["2026-07-10T11:32:07"] == 0.0
    assert got["2026-07-21T05:56:17"] == 2.6982            # on ₹10L
    assert got["2026-07-21T14:32:30"] == 2.6982            # AT the clean sheet: continues its own line
    assert got["2026-07-23T05:45:10"] == 2.7695            # on ₹2L after the clean sheet
    assert got["2026-08-04T05:07:11"] == 19.712
    assert got["2026-08-07T16:41:19"] == 19.712            # AT the injection: continuous (T1, chained)
    assert got["2026-09-28T11:44:41"] == round((1.19712 * 1_127_100.62 / 1_039_423.99 - 1) * 100, 4)
    assert [e["kind"] for e in h["capital_events"]] == ["clean_sheet", "capital_injection"]


def test_a_shadow_starts_at_zero_on_its_opening_day_and_net_rows_are_read(tmp_path):
    h = d.equity_history(_book(tmp_path))
    assert _pcts(h, "PAPER_2L") == {"2026-09-21T12:06:35": 0.0, "2026-09-25T15:28:58": 3.3481}
    assert h["net"] == [{"account": "PAPER_2L", "ts": "2026-10-09T09:30:00", "equity": 209000.0, "pct": 4.5}]


def test_an_unreadable_capital_move_stops_the_line_named_never_guessed(tmp_path):
    h = d.equity_history(_book(tmp_path, injection_detail="Rs. some amount"))
    got = _pcts(h)
    assert "2026-08-04T05:07:11" in got and "2026-08-07T16:41:19" not in got and "2026-09-28T11:44:41" not in got
    assert any("PAPER_10L's realized line stops at 2026-08-07T16:41:19" in n for n in h["notes"])


def test_no_net_history_yet_is_said_not_invented(tmp_path):
    p = tmp_path / "bm.db"
    c = brain_map.connect(str(p))
    pm.get_account(c)
    c.close()
    h = d.equity_history(p)
    assert h["net"] == [] and any("no true-net-equity history yet" in n for n in h["notes"])
    assert "error" in d.equity_history(tmp_path / "absent.db")


def test_window_start_and_the_carried_in_line():
    now = datetime(2026, 10, 9, 14, 0)
    assert d.window_start("Today", now) == "2026-10-09T00:00:00"
    assert d.window_start("1W", now) == "2026-10-02T14:00:00"
    assert d.window_start("1M", now) == "2026-09-09T14:00:00"
    assert d.window_start("YTD", now) == "2026-01-01T00:00:00"
    assert d.window_start("All time", now) is None
    pts = [{"account": "A", "ts": "2026-10-01T10:00:00", "pct": 1.0},
           {"account": "A", "ts": "2026-10-05T10:00:00", "pct": 2.0},
           {"account": "B", "ts": "2026-09-01T10:00:00", "pct": 5.0},
           {"account": "A", "ts": "2026-10-09T10:00:00", "pct": 3.0}]
    got = d.in_window(pts, "2026-10-08T00:00:00")
    assert {(p["account"], p["ts"], p["pct"]) for p in got} == {
        ("A", "2026-10-08T00:00:00", 2.0), ("B", "2026-10-08T00:00:00", 5.0), ("A", "2026-10-09T10:00:00", 3.0)}
    assert d.in_window(pts, None) == pts


def test_realized_lines_run_to_now_never_a_net_sample():
    pts = [{"account": "A", "ts": "2026-10-01T10:00:00", "pct": 1.0},
           {"account": "A", "ts": "2026-10-05T10:00:00", "pct": 2.0},
           {"account": "B", "ts": "2026-10-09T13:59:00", "pct": 5.0}]
    got = d.extend_to_now(pts, datetime(2026, 10, 9, 14, 0))
    assert got[:3] == pts
    assert {(p["account"], p["ts"], p["pct"]) for p in got[3:]} == {
        ("A", "2026-10-09T14:00:00", 2.0), ("B", "2026-10-09T14:00:00", 5.0)}
