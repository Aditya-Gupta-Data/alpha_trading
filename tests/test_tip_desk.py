"""
#tip-desk (decision #189): owner-pasted tips → parser + 120 s buffer → live contract → premium sizing → a
1-tap PAPER card → PAPER_TIP_SANDBOX, managed by stop / T1-T2-T3 / 15:15 square-off. Offline: fake chains,
tmp databases, injected notifier and clock; no network, no real data/ or logs/.

    python -m pytest tests/test_tip_desk.py -q
"""
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, exposure_gate, node_role, portfolio_manager as pm  # noqa: E402
from src.tip_desk import (ACCOUNT, CHATTER, DEFINED_SPREAD, EXIT_SIGNAL, IGNORED_KINDS, INCOMPLETE,  # noqa: E402
                          IST, OPTION_BUY, UNSAFE_REJECT, card as card_mod, desk, manager, parser, resolver,
                          sizing, store)
from src.tip_desk.buffer import TipBuffer  # noqa: E402

CFG = dict(config.TIP_DESK_DEFAULTS)
TODAY = date(2026, 11, 25)                     # a Wednesday, a NIFTY weekly expiry in this fixture
NOW = datetime(2026, 11, 25, 10, 0, tzinfo=IST)


def _db(tmp_path):
    conn = sqlite3.connect(tmp_path / "brain.db")
    pm.ensure_accounts_schema(conn)
    store.ensure_schema(conn)
    return conn


def _chain(quotes: dict, spot: float = 25350.0) -> dict:
    """quotes: {(strike, 'CE'|'PE'): (bid, ask, ltp)}."""
    oc = {}
    for (k, ot), (bid, ask, ltp) in quotes.items():
        oc.setdefault(f"{float(k):.6f}", {})[ot.lower()] = {"top_bid_price": bid, "top_ask_price": ask,
                                                             "last_price": ltp}
    return {"last_price": spot, "oc": oc}


def _fakes(quotes, expiries=("2026-11-25", "2026-12-02", "2026-12-30")):
    state = {"chain": _chain(quotes)}
    return (lambda und: list(expiries)), (lambda und, exp: state["chain"]), state


SENT = []


def _notify(payload):
    SENT.append(payload)


# ------------------------------------------------------------- the owner's samples
@pytest.mark.parametrize("text", ["BSE, MCX, ANGELONE, CDSL All Flying.", "Great profits running 🚀",
                                  "SENSEX Index Option Trade is Arriving"])
def test_sample_chatter_is_ignored(text):
    assert parser.classify(text)["kind"] in IGNORED_KINDS


def test_sample_unsafe_put_seller_rollover():
    assert parser.classify("Swiggy put seller can rollover to next month same strike")["kind"] == UNSAFE_REJECT


def test_sample_unsafe_split_ce_sell_joins_its_context():
    buf = TipBuffer(120)
    first = buf.add("m1", "Angle one BSE", now=0)
    assert first["parsed"]["kind"] in IGNORED_KINDS
    second = buf.add("m2", "CE SELL COULD BE DONE", now=20)
    assert second["parsed"]["kind"] == UNSAFE_REJECT
    assert second["group_id"] == "m1" and second["message_ids"] == ["m1", "m2"]


@pytest.mark.parametrize("text", ["Place limit orders in CG POWER", "WAITING FOR ENTRY"])
def test_sample_incomplete_is_ignored(text):
    assert parser.classify(text)["kind"] == INCOMPLETE


def test_sample_two_part_tip_becomes_one_option_buy():
    buf = TipBuffer(120)
    a = buf.add("m1", "Nifty TODAYS 25400 ce buy @25-28", now=0, today=TODAY)
    assert a["parsed"]["kind"] == OPTION_BUY and a["is_update"] is False and a["changed"] is True
    b = buf.add("m2", "Sl 5 tgt 54 78 110", now=40, today=TODAY)
    p = b["parsed"]
    assert b["group_id"] == "m1" and b["is_update"] is True and b["changed"] is True
    assert p["kind"] == OPTION_BUY and p["underlying"] == "NIFTY 50"
    assert p["legs"] == [{"side": "BUY", "strike": 25400.0, "option_type": "CE"}]
    assert (p["entry_low"], p["entry_high"], p["sl"], p["targets"]) == (25.0, 28.0, 5.0, [54.0, 78.0, 110.0])
    assert p["expiry_hint"]["kind"] == "nearest"


def test_sample_booking_now_is_an_exit_signal():
    assert parser.classify("Booking now")["kind"] == EXIT_SIGNAL


# ------------------------------------------------------------- parser + buffer rules
def test_parser_debit_spreads_allowed_credit_and_futures_rejected():
    bull = parser.classify("BUY NIFTY 25400 CE SELL 25600 CE @ 40 SL 20 TGT 80")
    assert bull["kind"] == DEFINED_SPREAD and bull["structure"] == "bull_call_spread"
    bear = parser.classify("Banknifty buy 55000 pe sell 54500 pe @ 120")
    assert bear["kind"] == DEFINED_SPREAD and bear["structure"] == "bear_put_spread"
    assert parser.classify("BUY NIFTY 25400 CE SELL 25200 CE @40")["kind"] == UNSAFE_REJECT
    assert parser.classify("Crude oil futures buy 6000")["kind"] == UNSAFE_REJECT
    assert parser.classify("Nifty 25400 CE sell @ 30")["kind"] == UNSAFE_REJECT


def test_buffer_window_closes_after_120_seconds():
    buf = TipBuffer(120)
    buf.add("m1", "Nifty TODAYS 25400 ce buy @25-28", now=0)
    late = buf.add("m2", "Sl 5 tgt 54 78 110", now=121)
    assert late["group_id"] == "m2" and late["parsed"]["kind"] == INCOMPLETE


def test_buffer_new_complete_tip_starts_its_own_group():
    buf = TipBuffer(120)
    buf.add("m1", "SENSEX Index Option Trade is Arriving", now=0)
    t = buf.add("m2", "Nifty TODAYS 25400 ce buy @25-28", now=10)
    assert t["group_id"] == "m2" and t["parsed"]["underlying"] == "NIFTY 50"


def test_buffer_follow_up_never_unmakes_a_trade():
    buf = TipBuffer(120)
    buf.add("m1", "Nifty TODAYS 25400 ce buy @25-28", now=0)
    r = buf.add("m2", "Sell half at 54", now=5)
    assert r["parsed"]["kind"] == OPTION_BUY and r["changed"] is False
    u = buf.add("m3", "25400 CE SELL COULD BE DONE", now=8)
    assert u["parsed"]["kind"] == UNSAFE_REJECT and u["group_id"] == "m3"


def test_exit_signal_stands_alone_even_right_after_a_tip():
    buf = TipBuffer(120)
    buf.add("m1", "Nifty TODAYS 25400 ce buy @25-28", now=0)
    e = buf.add("m2", "Booking now", now=30)
    assert e["parsed"]["kind"] == EXIT_SIGNAL and e["group_id"] == "m2"


# ------------------------------------------------------------- sizing + stale gate
def test_sizing_is_by_total_premium_with_caps():
    s = sizing.size(28.0, 65, 1, 20000, 0, CFG)        # per-tip cap 10k: floor((10000-40)/1820) = 5
    assert s["lots"] == 5 and s["premium_total_rs"] == 9100.0 and s["max_loss_rs"] == 9140.0
    s = sizing.size(28.0, 65, 1, 20000, 15000, dict(CFG, max_per_tip_rs=20000))   # room 5000 → 2 lots
    assert s["lots"] == 2
    big = sizing.size(400.0, 65, 1, 20000, 0, dict(CFG, max_per_tip_rs=20000))   # 26,000 a lot
    assert big["lots"] == 0 and "1 lot costs Rs.26,000" in big["reason"]
    t = sizing.size_tip(28.0, 65, 1, 20000, 0, CFG)
    assert t["n_tranches"] == 2 and t["lots"] == 5 and t["fees_rs"] == 60.0
    assert sizing.tranches(5) == [("A", 2, "book_at_t1"), ("B", 3, "trail")]
    assert sizing.tranches(1) == [("ALL", 1, "trail")]


def test_stale_price_gate():
    assert sizing.stale(35.0, 25, 28, 10)["stale"] is True
    assert sizing.stale(30.0, 25, 28, 10)["stale"] is False
    assert sizing.stale(20.0, 25, 28, 10)["vs_band"] == "below"


# ------------------------------------------------------------- resolver
def test_resolver_todays_is_the_nearest_live_expiry_and_strikes_must_exist():
    exp_fn, chain_fn, _ = _fakes({(25400, "CE"): (30.0, 31.0, 30.5)})
    p = parser.classify("Nifty TODAYS 25400 ce buy @25-28", TODAY)
    r = resolver.resolve(p, TODAY, exp_fn, chain_fn)
    assert r["ok"] and r["expiry"] == "2026-11-25" and r["cost_ps"] == 31.0 and r["lot_size"] == 65
    missing = resolver.resolve(parser.classify("Nifty TODAYS 25500 ce buy @25-28"), TODAY, exp_fn, chain_fn)
    assert not missing["ok"] and "strike not in the 2026-11-25 chain" in missing["reason"]


def test_resolver_names_what_it_cannot_trade():
    exp_fn, chain_fn, _ = _fakes({})
    sensex = resolver.resolve(parser.classify("Sensex 85000 ce buy @ 200"), TODAY, exp_fn, chain_fn)
    assert not sensex["ok"] and "BSE" in sensex["reason"]
    cg = resolver.resolve(parser.classify("CG POWER 700 CE buy @ 12"), TODAY, exp_fn, chain_fn)
    assert not cg["ok"] and "option universe" in cg["reason"]
    bad_date = resolver.resolve(parser.classify("Nifty 3 DEC 25400 ce buy @25", TODAY), TODAY, exp_fn, chain_fn)
    assert not bad_date["ok"] and "not in Dhan's list" in bad_date["reason"]


# ------------------------------------------------------------- desk: card → confirm → ledger
def test_ingest_cards_a_stale_tip_and_logs_it(tmp_path):
    conn = _db(tmp_path)
    exp_fn, chain_fn, _ = _fakes({(25400, "CE"): (34.0, 35.0, 34.5)})
    r = desk.ingest(conn, "m1", "Nifty TODAYS 25400 ce buy @25-28\nSl 5 tgt 54 78 110", now=NOW,
                    expiries_fn=exp_fn, chain_fn=chain_fn, cfg=CFG)
    assert r["action"] == "card" and r["card"]["stale"]["stale"] is True
    view = card_mod.trade_card(r["card"])
    assert view["title"].startswith("⚠️ STALE PRICE") and view["can_confirm"]
    assert any("Expires today" in l for l in view["lines"])
    assert [e["event"] for e in store.ledger(conn, "m1")] == ["carded"]


def test_ignored_and_unsafe_tips_are_still_logged(tmp_path):
    conn = _db(tmp_path)
    assert desk.ingest(conn, "c1", "Great profits running 🚀", now=NOW, cfg=CFG)["action"] == "ignore"
    assert desk.ingest(conn, "u1", "Swiggy put seller can rollover", now=NOW, cfg=CFG)["action"] == "reject"
    assert [(e["event"], e["kind"]) for e in store.ledger(conn)] == [("ignored", CHATTER),
                                                                    ("rejected_unsafe", UNSAFE_REJECT)]


def test_confirm_paper_opens_two_tranches_inside_the_cap(tmp_path):
    conn = _db(tmp_path)
    exp_fn, chain_fn, _ = _fakes({(25400, "CE"): (27.0, 28.0, 27.5)})
    r = desk.ingest(conn, "m1", "Nifty TODAYS 25400 ce buy @25-28\nSl 5 tgt 54 78 110", now=NOW,
                    expiries_fn=exp_fn, chain_fn=chain_fn, cfg=CFG)
    out = desk.confirm(conn, r["card_id"], "paper", now=NOW + timedelta(minutes=1), chain_fn=chain_fn,
                       market_open_fn=lambda n: True, cfg=CFG, notify_fn=_notify)
    assert out["ok"] and out["lots"] == 5 and out["fill_ps"] == 28.0
    rows = store.positions(conn)
    assert [(p["tranche"], p["lots"], p["rule"]) for p in rows] == [("A", 2, "book_at_t1"), ("B", 3, "trail")]
    assert pm.paper_locked_margin(conn, ACCOUNT) == out["max_loss_rs"] <= CFG["max_open_premium_rs"]
    assert store.get_card(conn, r["card_id"])["status"] == "confirmed"
    again = desk.confirm(conn, r["card_id"], "paper", now=NOW, chain_fn=chain_fn, market_open_fn=lambda n: True,
                         cfg=CFG, notify_fn=_notify)
    assert not again["ok"] and again["status"] == "confirmed"


def test_confirm_live_is_refused_paper_only(tmp_path):
    conn = _db(tmp_path)
    exp_fn, chain_fn, _ = _fakes({(25400, "CE"): (27.0, 28.0, 27.5)})
    r = desk.ingest(conn, "m1", "Nifty TODAYS 25400 ce buy @25-28", now=NOW, expiries_fn=exp_fn, chain_fn=chain_fn,
                    cfg=CFG)
    out = desk.confirm(conn, r["card_id"], "live", now=NOW, chain_fn=chain_fn, market_open_fn=lambda n: True,
                       cfg=CFG)
    assert not out["ok"] and out["status"] == "paper_only" and store.positions(conn) == []


def test_cancel_expired_and_closed_market_cards_never_fill(tmp_path):
    conn = _db(tmp_path)
    exp_fn, chain_fn, _ = _fakes({(25400, "CE"): (27.0, 28.0, 27.5)})
    c1 = desk.ingest(conn, "m1", "Nifty TODAYS 25400 ce buy @25-28", now=NOW, expiries_fn=exp_fn,
                     chain_fn=chain_fn, cfg=CFG)["card_id"]
    assert desk.cancel(conn, c1)["ok"]
    assert desk.confirm(conn, c1, now=NOW, chain_fn=chain_fn, market_open_fn=lambda n: True, cfg=CFG)["status"] \
        == "cancelled"
    c2 = desk.ingest(conn, "m2", "Nifty TODAYS 25400 ce buy @25-28", now=NOW, expiries_fn=exp_fn,
                     chain_fn=chain_fn, cfg=CFG)["card_id"]
    assert desk.confirm(conn, c2, now=NOW + timedelta(minutes=16), chain_fn=chain_fn,
                        market_open_fn=lambda n: True, cfg=CFG)["status"] == "expired"
    c3 = desk.ingest(conn, "m3", "Nifty TODAYS 25400 ce buy @25-28", now=NOW, expiries_fn=exp_fn,
                     chain_fn=chain_fn, cfg=CFG)["card_id"]
    assert desk.confirm(conn, c3, now=NOW, chain_fn=chain_fn, market_open_fn=lambda n: False,
                        cfg=CFG)["status"] == "market_closed"
    assert store.positions(conn, state=None) == []


def test_levels_arriving_after_confirm_update_the_open_rows(tmp_path):
    conn = _db(tmp_path)
    exp_fn, chain_fn, _ = _fakes({(25400, "CE"): (27.0, 28.0, 27.5)})
    r = desk.ingest(conn, "m1", "Nifty TODAYS 25400 ce buy @25-28", now=NOW, expiries_fn=exp_fn, chain_fn=chain_fn,
                    cfg=CFG)
    desk.confirm(conn, r["card_id"], now=NOW, chain_fn=chain_fn, market_open_fn=lambda n: True, cfg=CFG,
                 notify_fn=_notify)
    u = desk.ingest(conn, "m1", "Nifty TODAYS 25400 ce buy @25-28\nSl 5 tgt 54 78 110", now=NOW, cfg=CFG)
    assert u["action"] == "levels_updated" and len(u["journal_refs"]) == 2
    assert all(p["current_sl"] == 5.0 and p["targets"] == [54.0, 78.0, 110.0] for p in store.positions(conn))


def test_ledger_is_append_only(tmp_path):
    conn = _db(tmp_path)
    desk.ingest(conn, "c1", "Great profits running", now=NOW, cfg=CFG)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE tip_ledger SET event = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM tip_ledger")


# ------------------------------------------------------------- manager rules
def _pos(**kw):
    base = {"journal_ref": "tip:m1:ALL", "expiry": "2026-11-25", "entry_premium": 28.0, "current_sl": 5.0,
            "targets": [54.0, 78.0, 110.0], "targets_hit": 0, "rule": "trail", "lots": 1, "lot_size": 65,
            "fees_rs": 40.0}
    return dict(base, **kw)


def test_manager_stop_targets_trail_and_square_off():
    ev = manager.evaluate
    assert ev(_pos(), 4.5, NOW, CFG)["reason"] == "stop_loss"
    assert ev(_pos(rule="book_at_t1"), 55.0, NOW, CFG)["reason"] == "t1_booked"
    t1 = ev(_pos(), 55.0, NOW, CFG)
    assert t1["action"] == "raise" and t1["current_sl"] == 28.0 and t1["targets_hit"] == 1     # stop to cost
    t2 = ev(_pos(current_sl=28.0, targets_hit=1), 80.0, NOW, CFG)
    assert t2["current_sl"] == 54.0 and t2["targets_hit"] == 2
    assert ev(_pos(current_sl=54.0, targets_hit=2), 111.0, NOW, CFG)["reason"] == "final_target"
    assert ev(_pos(current_sl=28.0, targets_hit=1), 27.0, NOW, CFG)["reason"] == "trailing_stop"
    sq = ev(_pos(), 40.0, NOW.replace(hour=15, minute=15), CFG)
    assert sq["reason"] == "square_off_expiry_day" and sq["exit_ps"] == 40.0
    assert ev(_pos(expiry="2026-11-24"), None, NOW, CFG) == dict(ev(_pos(expiry="2026-11-24"), None, NOW, CFG),
                                                                 action="exit", reason="expired", exit_ps=0.0)
    assert ev(_pos(current_sl=None), None, NOW, CFG)["action"] == "hold"


def test_tick_books_t1_moves_b_to_cost_and_settles_through_the_door(tmp_path):
    conn = _db(tmp_path)
    exp_fn, chain_fn, state = _fakes({(25400, "CE"): (27.0, 28.0, 27.5)})
    r = desk.ingest(conn, "m1", "Nifty TODAYS 25400 ce buy @25-28\nSl 5 tgt 54 78 110", now=NOW,
                    expiries_fn=exp_fn, chain_fn=chain_fn, cfg=CFG)
    desk.confirm(conn, r["card_id"], now=NOW, chain_fn=chain_fn, market_open_fn=lambda n: True, cfg=CFG,
                 notify_fn=_notify)
    state["chain"] = _chain({(25400, "CE"): (56.0, 57.0, 56.5)})
    manager.tick(conn, now=NOW + timedelta(minutes=30), chain_fn=chain_fn, cfg=CFG, notify_fn=_notify)
    a = store.position(conn, "tip:m1:A")
    b = store.position(conn, "tip:m1:B")
    assert a["state"] == "closed" and a["exit_reason"] == "t1_booked"
    assert a["pnl_net"] == round((56.0 - 28.0) * 65 * 2 - a["fees_rs"], 2)
    assert b["state"] == "open" and b["current_sl"] == 28.0
    acct = pm.paper_account_summary(conn, ACCOUNT)
    assert acct["realized_pnl"] == a["pnl_net"]
    state["chain"] = _chain({(25400, "CE"): (0.0, 0.5, 0.2)})          # 15:15 on expiry day, near zero
    manager.tick(conn, now=NOW.replace(hour=15, minute=16), chain_fn=chain_fn, cfg=CFG, notify_fn=_notify)
    b = store.position(conn, "tip:m1:B")
    assert b["state"] == "closed" and b["exit_reason"] in ("trailing_stop", "square_off_expiry_day")
    assert pm.paper_locked_margin(conn, ACCOUNT) == 0.0


def test_zero_dte_worst_case_loss_is_the_premium_plus_fees(tmp_path):
    conn = _db(tmp_path)
    exp_fn, chain_fn, state = _fakes({(25400, "CE"): (27.0, 28.0, 27.5)})
    cfg = dict(CFG, max_per_tip_rs=20000)
    r = desk.ingest(conn, "m1", "Nifty TODAYS 25400 ce buy @25-28", now=NOW, expiries_fn=exp_fn, chain_fn=chain_fn,
                    cfg=cfg)
    out = desk.confirm(conn, r["card_id"], now=NOW, chain_fn=chain_fn, market_open_fn=lambda n: True, cfg=cfg,
                       notify_fn=_notify)
    assert out["max_loss_rs"] <= 20000
    state["chain"] = _chain({})                                       # no chain at all, then the day is gone
    manager.tick(conn, now=NOW + timedelta(days=1), chain_fn=chain_fn, cfg=cfg, notify_fn=_notify)
    acct = pm.paper_account_summary(conn, ACCOUNT)
    assert acct["realized_pnl"] == -out["max_loss_rs"] and acct["equity"] >= 0


def test_exit_now_closes_every_tranche_of_the_tip(tmp_path):
    conn = _db(tmp_path)
    exp_fn, chain_fn, state = _fakes({(25400, "CE"): (27.0, 28.0, 27.5)})
    r = desk.ingest(conn, "m1", "Nifty TODAYS 25400 ce buy @25-28", now=NOW, expiries_fn=exp_fn, chain_fn=chain_fn,
                    cfg=CFG)
    desk.confirm(conn, r["card_id"], now=NOW, chain_fn=chain_fn, market_open_fn=lambda n: True, cfg=CFG,
                 notify_fn=_notify)
    prompt = desk.ingest(conn, "x1", "Booking now", now=NOW, cfg=CFG)
    assert prompt["action"] == "exit_prompt" and prompt["positions"][0]["group_id"] == "m1"
    state["chain"] = _chain({(25400, "CE"): (40.0, 41.0, 40.5)})
    out = desk.exit_now(conn, "m1", now=NOW, chain_fn=chain_fn, market_open_fn=lambda n: True, notify_fn=_notify)
    assert out["ok"] and all(c["closed"] for c in out["closed"]) and store.positions(conn) == []
    assert desk.exit_now(conn, "m1", now=NOW, chain_fn=chain_fn, market_open_fn=lambda n: True)["ok"] is False


# ------------------------------------------------------------- the firewall
def test_sandbox_is_isolated_from_every_other_book(tmp_path):
    conn = _db(tmp_path)
    assert ACCOUNT in pm.PAPER_ACCOUNTS and ACCOUNT in pm.ISOLATED_ACCOUNTS
    assert ACCOUNT not in pm.shadow_account_ids()
    assert pm.paper_account_summary(conn, ACCOUNT)["starting_capital"] == 20000.0
    primary_before = pm.paper_equity(conn, pm.ACCOUNT_PAPER_10L)
    exp_fn, chain_fn, state = _fakes({(25400, "CE"): (27.0, 28.0, 27.5)})
    r = desk.ingest(conn, "m1", "Nifty TODAYS 25400 ce buy @25-28", now=NOW, expiries_fn=exp_fn, chain_fn=chain_fn,
                    cfg=CFG)
    desk.confirm(conn, r["card_id"], now=NOW, chain_fn=chain_fn, market_open_fn=lambda n: True, cfg=CFG,
                 notify_fn=_notify)
    # the open tip lock has no journal row, so it never fills the firm-wide #68 slot
    assert exposure_gate.conflicting_positions("NIFTY 50", "bullish", entries=[], conn=conn) == []
    state["chain"] = _chain({(25400, "CE"): (0.0, 0.5, 0.1)})
    desk.exit_now(conn, "m1", now=NOW, chain_fn=chain_fn, market_open_fn=lambda n: True, notify_fn=_notify)
    assert pm.paper_account_summary(conn, ACCOUNT)["realized_pnl"] < 0
    assert pm.paper_equity(conn, pm.ACCOUNT_PAPER_10L) == primary_before
    assert pm.entry_halt(conn, ACCOUNT) is None                       # #189: no daily breaker on the sandbox
    pm.paper_latch_halt(conn, ACCOUNT, why="test")
    assert pm.entry_halt(conn, ACCOUNT) is not None
    assert pm.entry_halt(conn, pm.ACCOUNT_PAPER_2L) is None


def test_manager_is_a_vm_only_execution_job():
    assert "src.tip_desk.manager" in node_role.EXECUTION_JOBS


def test_card_text_carries_the_order_details_and_no_live_path(tmp_path):
    conn = _db(tmp_path)
    exp_fn, chain_fn, _ = _fakes({(25400, "CE"): (27.0, 28.0, 27.5)})
    r = desk.ingest(conn, "m1", "Nifty TODAYS 25400 ce buy @25-28\nSl 5 tgt 54 78 110", now=NOW,
                    expiries_fn=exp_fn, chain_fn=chain_fn, cfg=CFG)
    text = "\n".join(card_mod.trade_card(r["card"])["lines"])
    assert "BUY NIFTY 50 25400CE 2026-11-25 qty 325 LIMIT 28" in text
    assert "book 2 lot(s) at T1" in text and "Paper only" in text


# ------------------------------------------------------------- the bot lane + the gateway
import asyncio  # noqa: E402
import os  # noqa: E402
from unittest import mock  # noqa: E402

from src.tip_desk import bot as tip_bot  # noqa: E402


class _Msg:
    def __init__(self, mid, text, author_id=42):
        self.id, self.content = mid, text
        self.author = type("A", (), {"id": author_id})()
        self.replies, self.reactions = [], []

    async def reply(self, content=None, **kw):
        self.replies.append(dict(kw, content=content))
        return self

    async def add_reaction(self, e):
        self.reactions.append(e)


def _run_bot(messages, answers):
    calls = []

    def bridge(method, path, payload):
        calls.append((path, payload))
        return 200, answers(path, payload)
    tip_bot.configure(bridge_call=bridge, is_owner=lambda uid: uid == 42)
    tip_bot._BUFFER["buf"] = TipBuffer(120)
    for m in messages:
        asyncio.run(tip_bot.handle_message(m))
    return calls


def test_bot_listens_only_to_the_owner_and_only_in_its_channel():
    with mock.patch.dict(os.environ, {"TIP_DESK_CHANNEL_ID": "123"}):
        assert tip_bot.is_tip_channel(123) and not tip_bot.is_tip_channel(999)
    with mock.patch.dict(os.environ, {"TIP_DESK_CHANNEL_ID": ""}):
        assert not tip_bot.is_tip_channel(123)
    stranger = _Msg(1, "Nifty TODAYS 25400 ce buy @25-28", author_id=7)
    assert _run_bot([stranger], lambda p, b: {}) == [] and stranger.replies == []


def test_bot_joins_the_split_tip_and_sends_chatter_alone():
    seen = []

    def answers(path, payload):
        seen.append(payload["text"])
        return {"action": "ignore"}
    m1 = _Msg(1, "Nifty TODAYS 25400 ce buy @25-28")
    m2 = _Msg(2, "Sl 5 tgt 54 78 110")
    m3 = _Msg(3, "Great profits running 🚀")
    calls = _run_bot([m1, m2, m3], answers)
    assert [c[1]["group_id"] for c in calls] == ["1", "1", "1"]
    assert seen == ["Nifty TODAYS 25400 ce buy @25-28", "Nifty TODAYS 25400 ce buy @25-28\nSl 5 tgt 54 78 110",
                    "Great profits running 🚀"]
    assert m3.reactions == ["🔇"]


def test_bot_renders_reject_and_exit_prompt():
    unsafe = _Msg(1, "Swiggy put seller can rollover to next month same strike")
    _run_bot([unsafe], lambda p, b: {"action": "reject", "reason": "unsafe: 'SELLER'"})
    assert unsafe.reactions == ["⛔"] and "UNSAFE" in unsafe.replies[0]["content"]
    ex = _Msg(2, "Booking now")
    _run_bot([ex], lambda p, b: {"action": "exit_prompt", "positions": [
        {"group_id": "9", "label": "NIFTY 50 25400CE 2026-11-25", "lots": 5, "entry_ps": 28.0, "last_mark": 40.0,
         "tranches": [{"tranche": "B", "current_sl": 28.0}]}]})
    assert "NIFTY 50 25400CE" in ex.replies[0]["content"] and ex.replies[0]["view"] is not None


def test_button_notes():
    assert "Not filled" in tip_bot.result_note("confirm", 200, {"ok": False, "reason": "card is expired"})
    assert "Exited" in tip_bot.result_note("exit", 200, {"ok": True, "closed": [
        {"journal_ref": "tip:9:A", "closed": True, "pnl": 1200.0}]})


def test_gateway_tipdesk_endpoints(tmp_path):
    from fastapi.testclient import TestClient
    from src import brain_map
    from src.api_server import app
    db = tmp_path / "brain.db"
    with mock.patch.dict(os.environ, {"API_KEY": "k"}), \
            mock.patch.object(brain_map, "connect", lambda *a, **k: sqlite3.connect(db)):
        c = TestClient(app)
        assert c.post("/api/tipdesk/ingest", json={"group_id": "1", "text": "hi"}).status_code == 401
        r = c.post("/api/tipdesk/ingest", headers={"X-API-Key": "k"},
                   json={"group_id": "1", "text": "WAITING FOR ENTRY", "message_ids": ["1"]})
        assert r.status_code == 200 and r.json()["action"] == "ignore"
        o = c.get("/api/tipdesk/open", headers={"X-API-Key": "k"}).json()
        assert o["ok"] and o["positions"] == [] and o["account"]["account_id"] == ACCOUNT
        live = c.post("/api/tipdesk/confirm", headers={"X-API-Key": "k"}, json={"card_id": "tipc:1", "mode": "live"})
        assert live.json()["ok"] is False


def test_hourly_orphan_reconcile_leaves_isolated_locks_alone(tmp_path, monkeypatch):
    import contextlib
    from src import journal, plan_tracker
    conn = _db(tmp_path)
    for acct, ref in ((pm.ACCOUNT_PAPER_EVENTS, "evt:abc123"), (ACCOUNT, "tip:1:ALL")):
        assert pm.paper_request_entry(conn, acct, ref, required_margin=1000.0, lots=1)["approved"]
    monkeypatch.setattr(journal, "read_all", lambda *a, **k: [])
    monkeypatch.setattr(journal, "locked", contextlib.nullcontext)
    out = plan_tracker.reconcile_orphan_locks(now=datetime.now() + timedelta(hours=2), conn=conn)
    assert out == {}
    assert pm.paper_locked_margin(conn, pm.ACCOUNT_PAPER_EVENTS) == 1000.0
    assert pm.paper_locked_margin(conn, ACCOUNT) == 1000.0
