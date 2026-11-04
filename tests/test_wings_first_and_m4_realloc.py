"""Wings-first in the OMS (#166/#179), recon alert de-dup, and the M4 IDX -> DARLINGS reallocation."""
import importlib.util
import itertools
import json
import sqlite3
from pathlib import Path

import pytest

from src import buckets, oms
from src import portfolio_manager as pm
from src.execution import paper_venue as pv
from src.execution import recon_engine as re_

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m1 = _load("m1_for_realloc", "scripts/migrate_m1_buckets.py")
ra = _load("reallocate_m4", "scripts/reallocate_m4_capital.py")


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    oms.ensure_schema(c)
    return c


def _condor(conn, kind=oms.ENTRY, tid="tkt:condor"):
    """Iron condor listed SELL-first on purpose: order of the list must not matter."""
    sides = [("SELL", "PE", 24800), ("SELL", "CE", 25200), ("BUY", "PE", 24600), ("BUY", "CE", 25400)]
    legs = [{"leg_id": f"{tid}:{i}", "leg_index": i, "side": s, "option_type": ot, "strike": k,
             "expiry": "2026-11-26", "qty_target": 65, "limit_price": 50.0, "fill_basis": "quoted",
             "correlation_id": oms.correlation_id_for(tid, i)} for i, (s, ot, k) in enumerate(sides)]
    oms.issue_ticket(conn, {"ticket_id": tid, "underlying": "NIFTY 50", "strategy": "iron_condor",
                            "lots": 1, "lot_size": 65, "kind": kind, "legs": legs})
    return tid


def _legs(conn, tid):
    return oms.ticket_view(conn, tid)["legs"]


def _by_side(conn, tid, side):
    return [l for l in _legs(conn, tid) if l["side"] == side]


# ------------------------------------------------------------ wings first

def test_sell_leg_is_refused_while_any_buy_leg_is_not_filled():
    c = _conn()
    tid = _condor(c)
    short = _by_side(c, tid, "SELL")[0]
    r = oms.apply_fill(c, short["leg_id"], 65, 50.0)
    assert not r["ok"] and r["reason"].startswith("wings_first")
    wing1, wing2 = _by_side(c, tid, "BUY")
    assert oms.apply_fill(c, wing1["leg_id"], 65, 10.0)["ok"]
    assert not oms.apply_fill(c, short["leg_id"], 65, 50.0)["ok"]
    assert oms.apply_fill(c, wing2["leg_id"], 30, 10.0)["state"] == oms.PARTIAL
    assert not oms.apply_fill(c, short["leg_id"], 65, 50.0)["ok"]
    assert oms.apply_fill(c, wing2["leg_id"], 35, 10.0)["state"] == oms.FILLED
    assert oms.apply_fill(c, short["leg_id"], 65, 50.0)["state"] == oms.FILLED


def test_every_fill_order_keeps_the_invariant():
    """Brute force: try all 24 orders of filling the condor's legs; no sequence ever leaves a
    SELL fill on the book while a BUY leg is unfilled."""
    for order in itertools.permutations(range(4)):
        c = _conn()
        tid = _condor(c)
        for _ in range(2):
            for i in order:
                oms.apply_fill(c, f"{tid}:{i}", 65, 10.0)
                assert oms.verify_wings_first_order(_legs(c, tid))
        assert oms.ticket_status(c, tid) == oms.FILLED


def test_rejected_wing_means_the_shorts_never_fill():
    c = _conn()
    tid = _condor(c)
    wing = _by_side(c, tid, "BUY")[0]
    oms.reject(c, wing["leg_id"], "venue refused")
    for short in _by_side(c, tid, "SELL"):
        assert not oms.apply_fill(c, short["leg_id"], 65, 50.0)["ok"]
    assert all(l["qty_filled"] == 0 for l in _by_side(c, tid, "SELL"))


def test_phase_names_the_wings_filled_gap():
    c = _conn()
    tid = _condor(c)
    assert oms.ticket_view(c, tid)["phase"] == oms.PENDING
    for w in _by_side(c, tid, "BUY"):
        oms.apply_fill(c, w["leg_id"], 65, 10.0)
    v = oms.ticket_view(c, tid)
    assert v["phase"] == oms.PARTIAL_WINGS_FILLED and v["status"] == oms.PARTIAL
    for s in _by_side(c, tid, "SELL"):
        oms.apply_fill(c, s["leg_id"], 65, 50.0)
    assert oms.ticket_view(c, tid)["phase"] == oms.FILLED


def test_single_leg_sell_tickets_are_unaffected():
    c = _conn()
    tid = "tkt:eqexit"
    oms.issue_ticket(c, {"ticket_id": tid, "underlying": "TCS", "strategy": "equity_long", "lots": 1,
                         "lot_size": 10, "kind": oms.EXIT,
                         "legs": [{"leg_id": f"{tid}:0", "leg_index": 0, "side": "SELL", "qty_target": 10,
                                   "limit_price": 3000.0, "correlation_id": oms.correlation_id_for(tid, 0)}]})
    assert oms.apply_fill(c, f"{tid}:0", 10, 3000.0)["state"] == oms.FILLED


def test_verify_wings_first_order_flags_a_naked_short():
    legs = [{"side": "BUY", "state": oms.PARTIAL, "qty_filled": 30},
            {"side": "SELL", "state": oms.PARTIAL, "qty_filled": 10}]
    assert not oms.verify_wings_first_order(legs)
    legs[1]["qty_filled"] = 0
    assert oms.verify_wings_first_order(legs)


def test_venue_fills_a_sell_first_listed_ticket_in_one_pass():
    c = _conn()
    tid = _condor(c)
    s = pv.sweep(c, slippage_fn=lambda u: 0.0, stamp=False)
    assert s["filled"] == 4 and s["refused"] == 0
    assert oms.ticket_status(c, tid) == oms.FILLED


# ------------------------------------------------------------ recon de-dup

def test_recon_announces_a_standing_mismatch_once(monkeypatch, tmp_path):
    fired = []
    from src import notifier
    monkeypatch.setattr(notifier, "fire_broadcast", lambda p: fired.append(p))
    from tests.test_recon_engine import _db, _read_fn
    positions = [{"securityId": "11536", "tradingSymbol": "TCS", "exchangeSegment": "NSE_EQ", "netQty": -5}]
    log = tmp_path / "r.jsonl"
    r1 = re_.run(conn=_db(), read_fn=_read_fn(positions), log_path=log, halt=False)
    r2 = re_.run(conn=_db(), read_fn=_read_fn(positions), log_path=log, halt=False)
    assert r1["verdict"] == r2["verdict"] == "mismatch"
    assert not r1["repeat_of_last"] and r2["repeat_of_last"]
    assert len(fired) == 1
    assert len(log.read_text().splitlines()) == 2
    more = positions + [{"securityId": "2885", "tradingSymbol": "INFY", "exchangeSegment": "NSE_EQ", "netQty": 3}]
    re_.run(conn=_db(), read_fn=_read_fn(more), log_path=log, halt=False)
    assert len(fired) == 2


def test_setup_cron_runs_recon_every_five_minutes_in_session():
    text = (ROOT / "scripts" / "setup_cron.sh").read_text()
    assert '*/5 9-14 * * 1-5 cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.execution.recon_engine' in text
    assert '0-30/5 15 * * 1-5 cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.execution.recon_engine' in text


# ------------------------------------------------------------ reallocation

def _bucket_db(budget_at_migration=200000.0, budget_now=300000.0):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pm.get_account(c)
    pm.get_paper_account(c, pm.ACCOUNT_PAPER_2L)
    c.execute("CREATE TABLE IF NOT EXISTS treasury_state (id INTEGER PRIMARY KEY CHECK (id = 1), "
              "equity_budget_rs REAL NOT NULL, updated_at TEXT NOT NULL)")
    c.execute("INSERT INTO treasury_state VALUES (1, ?, 'now')", (budget_at_migration,))
    c.commit()
    m1.apply(c, m1.plan(c))
    c.execute("UPDATE treasury_state SET equity_budget_rs = ?", (budget_now,))
    c.commit()
    return c


def test_reallocation_moves_one_lakh_and_keeps_parity_and_drawdowns():
    c = _bucket_db()
    firm = ra._firm_equity(c)
    idx_dd = buckets.drawdown_pct(buckets.bucket(c, ra.IDX))
    p = ra.plan(c)
    assert p["ok"] and p["delta_rs"] == 100000.0
    res = ra.apply(c, p)
    assert res["firm_equity_rs"] == firm
    assert buckets.bucket(c, ra.DARL)["starting_capital"] == 300000.0
    assert buckets.drawdown_pct(buckets.bucket(c, ra.IDX)) == idx_dd
    assert buckets.drawdown_pct(buckets.bucket(c, ra.DARL)) == 0.0
    row = c.execute("SELECT portfolio_id, delta_rs, from_portfolio_id FROM capital_allocations").fetchone()
    assert tuple(row) == (ra.DARL, 100000.0, ra.IDX)
    assert c.execute("SELECT COUNT(*) FROM account_events WHERE event_type = 'm4_capital_reallocation'").fetchone()[0] == 1
    again = ra.plan(c)
    assert again.get("noop") and not again["ok"]


def test_reallocation_refuses_to_move_quarantined_capital():
    c = _bucket_db()
    buckets.latch_bucket_halt(c, ra.IDX, why="test")
    p = ra.plan(c)
    assert not p["ok"] and "quarantined" in p["reason"]
    with pytest.raises(RuntimeError):
        ra.apply(c, p)


def test_reallocation_dry_run_writes_nothing(tmp_path, capsys):
    db = tmp_path / "bm.db"
    c = _bucket_db()
    disk = sqlite3.connect(db)
    c.backup(disk)
    disk.close()
    assert ra.main(["--db", str(db)]) == 0
    assert '"delta_rs": 100000.0' in capsys.readouterr().out
    chk = sqlite3.connect(db)
    assert chk.execute("SELECT starting_capital FROM portfolios WHERE portfolio_id = ?",
                       (ra.DARL,)).fetchone()[0] == 200000.0
    assert ra.main(["--db", str(db), "--apply"]) == 0
    assert chk.execute("SELECT starting_capital FROM portfolios WHERE portfolio_id = ?",
                       (ra.DARL,)).fetchone()[0] == 300000.0


# ------------------------------------------------------------ Day-1 audit fixes (#180)

def test_recon_log_writes_the_paper_book_once_until_it_changes(tmp_path):
    from src.dashboard import data as dd
    from tests.test_recon_engine import _db, _read_fn
    log = tmp_path / "r.jsonl"
    for _ in range(3):
        re_.run(conn=_db(), read_fn=_read_fn(), log_path=log, announce=False, halt=False)
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    assert rows[0]["paper_only"] and "paper_only_ref" not in rows[0]
    assert rows[1]["paper_only"] == [] and rows[1]["paper_only_ref"] == rows[0]["ts"]
    assert rows[2]["paper_only_ref"] == rows[0]["ts"]
    assert rows[2]["paper_only_count"] == len(rows[0]["paper_only"])
    assert len(log.read_text().splitlines()[2]) < len(log.read_text().splitlines()[0])
    latest = dd.latest_recon(log)
    assert latest["paper_only"] == rows[0]["paper_only"]


def test_recon_log_rewrites_the_paper_book_when_it_changes(tmp_path):
    from tests.test_recon_engine import _db, _read_fn
    log = tmp_path / "r.jsonl"
    re_.run(conn=_db(), read_fn=_read_fn(), log_path=log, announce=False, halt=False)
    re_.run(conn=_db(with_2l=False), read_fn=_read_fn(), log_path=log, announce=False, halt=False)
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    assert rows[1]["paper_only_sig"] != rows[0]["paper_only_sig"]
    assert rows[1]["paper_only"] and "paper_only_ref" not in rows[1]


def test_gem_name_index_matches_listed_names_exactly():
    from src.ingestion.alt_data import gem_tenders as g
    master = ("SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_TRADING_SYMBOL,"
              "SEM_CUSTOM_SYMBOL,SEM_SERIES,SM_SYMBOL_NAME\n"
              "NSE,E,3150,EQUITY,SIEMENS,Siemens,EQ,SIEMENS LTD\n"
              "NSE,E,6364,EQUITY,NATIONALUM,NALCO,EQ,NATIONAL ALUMINIUM CO LTD\n"
              "NSE,E,1,EQUITY,ACMESOLAR,Acme Solar Holdings,EQ,ACME SOLAR HOLDINGS LTD\n"
              "NSE,E,2,EQUITY,TWINA,Twin Corp,EQ,TWIN CORP LTD\n"
              "NSE,E,3,EQUITY,TWINB,Twin Corp,EQ,TWIN CORPORATION LIMITED\n"
              "NSE,E,4,EQUITY,SMEX,Sme Co,SM,SME EXAMPLE LTD\n"
              "BSE,E,5,EQUITY,BSEONLY,Bse Only,A,BSE ONLY LTD\n")
    idx = g.build_name_index(master)
    m = lambda n: g.match_listed_ticker(n, idx)
    assert m("SIEMENS LIMITED") == "SIEMENS.NS"
    assert m("M/S Siemens Ltd.( MSE Social Category:General )") == "SIEMENS.NS"
    assert m("NATIONAL ALUMINIUM COMPANY LIMITED") == "NATIONALUM.NS"
    assert m("SIEMENS HEALTHCARE PRIVATE LIMITED") == ""
    assert m("ACME VENTURES") == ""
    assert m("TWIN CORPORATION LIMITED") == ""          # two symbols share the name: ambiguous, dropped
    assert m("SME EXAMPLE LTD") == "" and m("BSE ONLY LTD") == ""
    assert m("BHARAT HEAVY ELECTRICALS LIMITED") == "BHEL.NS"   # the pattern list still runs first


def test_gem_default_index_is_never_read_under_pytest():
    from src.ingestion.alt_data import gem_tenders as g
    assert g.load_name_index() == {}


def test_name_index_refresh_is_weekly_and_keeps_the_old_file_on_failure(tmp_path):
    from src.ingestion.alt_data import gem_tenders as g
    path = tmp_path / "names.json"
    rows = "\n".join(f"NSE,E,{i},EQUITY,SYM{i},Company Number {i},EQ,COMPANY NUMBER {i} LTD" for i in range(1200))
    master = ("SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_TRADING_SYMBOL,"
              "SEM_CUSTOM_SYMBOL,SEM_SERIES,SM_SYMBOL_NAME\n" + rows + "\n")
    r = g.refresh_name_index(path, fetch_fn=lambda url: master, now=1_000_000.0)
    assert r["refreshed"] and r["names"] == 1200
    assert g.load_name_index(path)["CO NUMBER 7"] == "SYM7"
    assert g.refresh_name_index(path, fetch_fn=lambda url: 1 / 0)["reason"] == "fresh"
    import os
    os.utime(path, (0, 0))
    bad = g.refresh_name_index(path, fetch_fn=lambda url: (_ for _ in ()).throw(OSError("cdn down")))
    assert not bad["refreshed"] and "cdn down" in bad["reason"]
    assert json.loads(path.read_text())["names"]["CO NUMBER 7"] == "SYM7"
