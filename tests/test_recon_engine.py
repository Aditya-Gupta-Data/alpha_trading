"""Decision #94 — the READ-ONLY recon engine. The first test is the
mechanical Rule-7 guard the decision promised: it fails the build if a write
verb, an order path or a placement SDK method ever appears in the module."""
import ast
import json
import re
import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.execution import recon_engine as re_


SRC = Path(re_.__file__).read_text()


def test_zero_write_or_order_endpoints_exist_in_the_module():
    # 1. the only HTTP verb, by AST: every attribute call on `requests` is .get
    tree = ast.parse(SRC)
    verbs = {node.func.attr for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and isinstance(node.func.value, ast.Name) and node.func.value.id == "requests"}
    assert verbs == {"get"}
    # 2. no write verb, order path or placement SDK method anywhere in the source
    forbidden = [r"requests\.(post|put|delete|patch)", r"\.post\(", r"\.put\(",
                 r"\.delete\(", r"\.patch\(", r"/orders", r"/super", r"/slicing",
                 r"place_order", r"placeOrder", r"modify_order", r"cancel_order",
                 r"place_slice", r"super_order", r"convert_position", r"dhanhq",
                 r"forever_order", r"httpx", r"urllib\.request"]
    for pat in forbidden:
        assert not re.search(pat, SRC), f"forbidden pattern in recon_engine: {pat}"
    # 3. the endpoint surface is exactly the three portfolio reads
    assert re_.ENDPOINTS == ("/positions", "/holdings", "/fundlimit")
    # 4. no write to brain_map: the only SQL verbs are SELECT / PRAGMA
    sql = re.findall(r'"(SELECT|PRAGMA|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b', SRC)
    assert set(sql) <= {"SELECT", "PRAGMA"}


def test_broker_is_never_dialled_under_pytest():
    out = re_.fetch_broker_book()
    assert out["reachable"] is False and out["positions"] == []
    assert any("muzzled" in e for e in out["errors"])


def _db(with_2l=True):
    from src import portfolio_manager as pm, oms
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pm.get_account(conn)
    pm.get_paper_account(conn, "PAPER_2L")
    oms.ensure_schema(conn)
    conn.execute("INSERT INTO margin_locks (journal_ref, margin_rs, locked_at) "
                 "VALUES ('ab12cd34', 50000, '2026-09-20T10:00:00')")
    conn.execute("INSERT INTO margin_locks (journal_ref, margin_rs, locked_at, released_at) "
                 "VALUES ('old00001', 1, '2026-09-01', '2026-09-02')")
    if with_2l:
        conn.execute("INSERT INTO paper_margin_locks (journal_ref, account_id, margin_rs, locked_at) "
                     "VALUES ('ab12cd34', 'PAPER_2L', 9000, '2026-09-20T10:00:00')")
    for tid, ref, kind, status in (("tkt:1", "ab12cd34", "ENTRY", "FILLED"),
                                   ("tkt:2", "zz99zz99", "ENTRY", "FILLED"),
                                   ("tkt:3", "zz99zz99", "EXIT", "FILLED"),
                                   ("tkt:4", "pend0001", "ENTRY", "PENDING")):
        conn.execute("INSERT INTO trade_tickets (ticket_id, journal_ref, underlying, strategy, "
                     "direction, lots, lot_size, reward_risk, source, status, issued_at, "
                     "updated_at, note, account_id, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (tid, ref, "RELIANCE.NS", "bear_put_spread", "bearish", 1, 500, 0.5,
                      "t", status, "2026-09-20", "2026-09-20", "", "PAPER_10L", kind))
    conn.commit()
    return conn


def test_paper_book_is_active_locks_plus_open_filled_entries_only():
    rows = re_.read_paper_book(conn=_db())
    keys = {(r["ref"], r["account"]) for r in rows}
    assert keys == {("ab12cd34", "PAPER_10L"), ("ab12cd34", "PAPER_2L")}
    r10 = next(r for r in rows if r["account"] == "PAPER_10L")
    assert r10["underlying"] == "RELIANCE.NS" and r10["ticket_id"] == "tkt:1"
    assert r10["margin_rs"] == 50000
    # released lock, exited ticket and pending ticket are all excluded


def _read_fn(positions=None, holdings=None, funds=None, fail=()):
    def fn(ep):
        if ep in fail:
            return None, f"{ep}: HTTP 500"
        return {"/positions": {"data": positions or []},
                "/holdings": holdings if holdings is not None else [],
                "/fundlimit": funds or {"availabelBalance": 1411.18, "utilizedAmount": 0}}[ep], None
    return fn


def test_empty_broker_book_against_a_paper_book_is_parity_with_paper_only_rows():
    with tempfile.TemporaryDirectory() as tmp:
        res = re_.run(conn=_db(), read_fn=_read_fn(), log_path=Path(tmp) / "r.jsonl",
                      announce=False)
    assert res["verdict"] == "parity" and res["broker_reachable"]
    assert res["mismatches"] == []
    assert {r["account"] for r in res["paper_only"]} == {"PAPER_10L", "PAPER_2L"}
    assert res["funds"]["available"] == 1411.18


def test_a_broker_position_the_book_cannot_explain_is_a_critical_mismatch(monkeypatch):
    fired = []
    from src import notifier
    monkeypatch.setattr(notifier, "fire_broadcast", lambda p: fired.append(p))
    positions = [{"securityId": "2885", "tradingSymbol": "RELIANCE", "exchangeSegment": "NSE_EQ",
                  "netQty": 10, "buyAvg": 1400.0, "productType": "CNC"},
                 {"securityId": "11536", "tradingSymbol": "TCS", "exchangeSegment": "NSE_EQ",
                  "netQty": -5, "sellAvg": 3000.0},
                 {"securityId": "1", "tradingSymbol": "FLAT", "exchangeSegment": "NSE_EQ",
                  "buyQty": 5, "sellQty": 5}]                     # net 0: ignored
    holdings = [{"securityId": "772", "tradingSymbol": "DABUR", "totalQty": 3}]
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "r.jsonl"
        res = re_.run(conn=_db(), read_fn=_read_fn(positions, holdings), log_path=log)
        assert json.loads(log.read_text().splitlines()[-1])["verdict"] == "mismatch"
    kinds = sorted((m["kind"], m["symbol"]) for m in res["mismatches"])
    assert kinds == [("broker_holding_unknown_to_book", "DABUR"),
                     ("broker_position_on_book_name_but_paper_never_ordered", "RELIANCE"),
                     ("broker_position_unknown_to_book", "TCS")]
    assert all(m["severity"] == "CRITICAL" and "🔴 RECON MISMATCH" in m["detail"]
               for m in res["mismatches"])
    assert res["broker_positions"] == 2
    assert fired and fired[0]["event"] == "recon_mismatch"
    assert "🔴 RECON MISMATCH" in re_.render(res)


def test_a_failed_positions_read_is_unknown_never_parity():
    res = re_.compare(re_.fetch_broker_book(read_fn=_read_fn(fail=("/positions",))),
                      re_.read_paper_book(conn=_db()))
    assert res["verdict"] == "unknown" and res["mismatches"] == [] and res["paper_only"] == []
    assert res["errors"] == ["/positions: HTTP 500"]


def test_dh1111_empty_holdings_is_an_empty_list_not_an_error():
    class R:
        status_code = 400
        text = '{"errorCode":"DH-1111","errorMessage":"No holdings"}'
        def json(self):
            return json.loads(self.text)
    import requests
    orig = requests.get
    requests.get = lambda *a, **k: R()
    try:
        assert re_._read("/holdings", {"access-token": "x"}) == ([], None)
    finally:
        requests.get = orig


def test_dry_run_reads_the_book_only():
    with tempfile.TemporaryDirectory() as tmp:
        res = re_.run(conn=_db(with_2l=False), dry_run=True,
                      log_path=Path(tmp) / "r.jsonl", announce=False)
    assert res["verdict"] == "unknown" and res["book_rows"] == 1
    assert any("dry run" in e for e in res["errors"])
