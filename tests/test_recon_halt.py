"""Decision #172: a CRITICAL recon divergence (ghost position/holding, a live or filled broker order) latches the
desk-wide halt — firm, equity desk, every latched paper account and bucket; parity, an unread broker, a dry run,
`halt=False` and inert orders latch nothing."""
from pathlib import Path

import pytest

from src import brain_map, buckets, equity_desk, notifier, portfolio_manager as pm
from src.execution import recon_engine as re_

GHOST = [{"securityId": "11536", "tradingSymbol": "TCS", "exchangeSegment": "NSE_EQ", "netQty": -5}]


def _seed(c, pid, account, cap, mdd):
    c.execute("INSERT INTO portfolios (portfolio_id, account_id, strategy_family, starting_capital, realized_pnl, "
              "peak_equity, risk_per_trade_pct, max_drawdown_pct, daily_loss_pct, created_at) "
              "VALUES (?, ?, 'index_options', ?, 0, ?, 2.0, ?, 3.0, 'now')", (pid, account, cap, cap, mdd))


@pytest.fixture
def conn():
    c = brain_map.connect(":memory:")
    pm.ensure_accounts_schema(c)
    pm.get_account(c)
    for a in (pm.ACCOUNT_PAPER_2L, pm.ACCOUNT_PAPER_SHADOW_LEARNER):
        pm.get_paper_account(c, a)
    _seed(c, "PAPER_10L/IDX_SPREADS", "PAPER_10L", 800_000.0, 10.0)
    _seed(c, "PAPER_SHADOW_LEARNER/IDX_SPREADS", "PAPER_SHADOW_LEARNER", 200_000.0, None)
    c.execute("INSERT INTO margin_locks (journal_ref, margin_rs, locked_at) VALUES ('ab12cd34', 50000, 'now')")
    c.commit()
    yield c
    c.close()


@pytest.fixture
def cards(monkeypatch):
    fired = []
    monkeypatch.setattr(notifier, "fire_broadcast", lambda p: fired.append(p))
    return fired


def _broker(positions=None, holdings=None, orders=None, fail=()):
    def fn(ep):
        if ep in fail:
            return None, f"{ep}: HTTP 500"
        return {"/positions": {"data": positions or []}, "/holdings": holdings or [],
                "/fundlimit": {"availabelBalance": 0}, "/orders": orders or []}[ep], None
    return fn


def _latched(c):
    return {"firm": pm.halt_latched(c), "desk": equity_desk.desk_halt_latched(c),
            "2L": pm.paper_halt_latched(c, pm.ACCOUNT_PAPER_2L),
            "learner": pm.paper_halt_latched(c, pm.ACCOUNT_PAPER_SHADOW_LEARNER),
            "idx": bool(buckets.bucket(c, "PAPER_10L/IDX_SPREADS")["halted_at"]),
            "learner_bucket": bool(buckets.bucket(c, "PAPER_SHADOW_LEARNER/IDX_SPREADS")["halted_at"])}


NONE = {"firm": False, "desk": False, "2L": False, "learner": False, "idx": False, "learner_bucket": False}
ALL = {"firm": True, "desk": True, "2L": True, "learner": False, "idx": True, "learner_bucket": False}


def test_ghost_position_latches_the_desk_wide_halt(conn, cards, tmp_path):
    res = re_.run(conn=conn, read_fn=_broker(positions=GHOST), log_path=tmp_path / "r.jsonl")
    assert res["verdict"] == "mismatch" and res["halt"]["tripped"] is True
    assert _latched(conn) == ALL
    assert pm.trading_halted(conn) and pm.paper_trading_halted(conn, pm.ACCOUNT_PAPER_2L)
    reason = conn.execute("SELECT detail FROM account_events WHERE event_type = ?", (pm.HALT_LATCH_EVENT,)
                          ).fetchone()[0]
    assert reason.startswith("recon divergence") and "TCS" in reason
    assert "⛔ desk-wide halt LATCHED" in cards[0]["fields"][0]["value"]
    assert "halt LATCHED by this recon" in re_.render(res)


def test_ghost_holding_latches(conn, cards, tmp_path):
    res = re_.run(conn=conn, read_fn=_broker(holdings=[{"securityId": "772", "tradingSymbol": "DABUR",
                                                         "totalQty": 3}]), log_path=tmp_path / "r.jsonl")
    assert res["mismatches"][0]["kind"] == "broker_holding_unknown_to_book" and _latched(conn) == ALL


@pytest.mark.parametrize("status", ["TRADED", "PART_TRADED", "PENDING", "TRANSIT"])
def test_a_live_or_filled_broker_order_is_a_ghost_and_latches(conn, cards, tmp_path, status):
    order = {"orderId": "9001", "tradingSymbol": "NIFTY", "exchangeSegment": "NSE_FNO", "transactionType": "SELL",
             "orderStatus": status, "quantity": 75, "filledQty": 75 if status == "TRADED" else 0}
    res = re_.run(conn=conn, read_fn=_broker(orders=[order]), log_path=tmp_path / "r.jsonl")
    m = res["mismatches"]
    assert [x["kind"] for x in m] == ["broker_order_unknown_to_book"] and m[0]["severity"] == "CRITICAL"
    assert res["broker_orders"] == 1 and _latched(conn) == ALL


@pytest.mark.parametrize("status", ["REJECTED", "CANCELLED", "EXPIRED"])
def test_an_inert_broker_order_is_reported_but_never_halts(conn, cards, tmp_path, status):
    order = {"orderId": "9002", "tradingSymbol": "NIFTY", "orderStatus": status, "quantity": 75, "filledQty": 0}
    res = re_.run(conn=conn, read_fn=_broker(orders=[order]), log_path=tmp_path / "r.jsonl")
    assert res["verdict"] == "parity" and res["halt"]["tripped"] is False
    assert any(r["kind"] == "broker_order_inert" for r in res["paper_only"])
    assert _latched(conn) == NONE and cards == []


def test_parity_with_paper_only_rows_never_halts(conn, cards, tmp_path):
    res = re_.run(conn=conn, read_fn=_broker(), log_path=tmp_path / "r.jsonl")
    assert res["verdict"] == "parity" and res["paper_only"] and _latched(conn) == NONE


def test_an_unread_broker_never_halts(conn, cards, tmp_path):
    res = re_.run(conn=conn, read_fn=_broker(positions=GHOST, fail=("/positions",)), log_path=tmp_path / "r.jsonl")
    assert res["verdict"] == "unknown" and res["halt"]["tripped"] is False and _latched(conn) == NONE


def test_halt_off_and_dry_run_report_without_latching(conn, cards, tmp_path):
    res = re_.run(conn=conn, read_fn=_broker(positions=GHOST), log_path=tmp_path / "r.jsonl", halt=False)
    assert res["verdict"] == "mismatch" and "halt" not in res and _latched(conn) == NONE
    res = re_.run(conn=conn, dry_run=True, log_path=tmp_path / "r.jsonl", announce=False)
    assert "halt" not in res and _latched(conn) == NONE


def test_a_second_divergence_keeps_one_latch(conn, cards, tmp_path):
    re_.run(conn=conn, read_fn=_broker(positions=GHOST), log_path=tmp_path / "r.jsonl")
    again = re_.run(conn=conn, read_fn=_broker(positions=GHOST), log_path=tmp_path / "r.jsonl")
    assert again["halt"]["tripped"] is True and not any(again["halt"]["targets"].values())
    assert conn.execute("SELECT COUNT(*) FROM account_events WHERE event_type = ?", (pm.HALT_LATCH_EVENT,)
                        ).fetchone()[0] == 1


def test_a_failed_trip_is_logged_and_carded_not_raised(conn, cards, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db locked")
    monkeypatch.setattr(pm, "latch_halt", boom)
    log = tmp_path / "r.jsonl"
    res = re_.run(conn=conn, read_fn=_broker(positions=GHOST), log_path=log)
    assert res["halt"]["tripped"] is False and "db locked" in res["halt"]["error"]
    assert Path(log).read_text().count("\n") == 1
    assert "halt trip FAILED" in cards[0]["fields"][0]["value"]


def test_cli_no_halt_flag_is_passed_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(re_, "run", lambda **k: seen.update(k) or {
        "ts": "t", "verdict": "parity", "broker_reachable": True, "broker_positions": 0, "broker_holdings": 0,
        "book_rows": 0, "errors": [], "mismatches": [], "paper_only": []})
    assert re_.main(["--no-halt", "--quiet"]) == 0 and seen["halt"] is False and seen["announce"] is False
    re_.main([])
    assert seen["halt"] is True
