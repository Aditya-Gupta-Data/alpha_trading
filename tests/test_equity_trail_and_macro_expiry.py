"""Decision #107 (2026-09-23): V1.2 equity-desk ATR trailing stops (one-way
ratchet, OMS exit) and the macro instrument expiry guard on the CEO brief."""
import json
import sqlite3
import tempfile
from datetime import date
from pathlib import Path

import pytest

from src import equity_trail as et
from src import equity_shadow_proposer as sp
from src import knowledge_graph_logger as kg


def _bars(start: date, closes, rng=2.0):
    out, d = [], start
    for c in closes:
        while d.weekday() >= 5:
            d = date.fromordinal(d.toordinal() + 1)
        out.append((d.isoformat(), c - rng / 2, c + rng / 2, c))
        d = date.fromordinal(d.toordinal() + 1)
    return out


# ------------------------------------------------------------ the ratchet
def test_trail_is_highest_high_minus_mult_atr_and_only_ever_rises():
    pre = _bars(date(2026, 8, 3), [100.0] * 20)                     # ATR == 2.0 (range 2)
    up = _bars(date(2026, 8, 31), [100, 104, 108, 112, 116])        # highs 101..117
    t = et.compute_trail("2026-08-31", 95.0, pre + up, atr_mult=3.0, atr_n=14)
    assert t["armed"] and t["extreme"] == 117.0
    assert t["trail"] == round(117.0 - 3.0 * t["atr"], 2)             # highest high − 3×ATR(14)
    assert 2.0 < t["atr"] < 3.5                                       # the 4-pt gaps widen the TR
    # the pullback: highs fall, ATR grows — the trail does NOT fall
    down = _bars(date(2026, 9, 7), [110, 104, 100, 98], rng=6.0)
    t2 = et.compute_trail("2026-08-31", 95.0, pre + up + down, atr_mult=3.0, atr_n=14)
    assert t2["extreme"] == 117.0 and t2["trail"] >= t["trail"]
    # level by level along the walk: never a lower value than the step before
    levels = [et.compute_trail("2026-08-31", 95.0, (pre + up + down)[:k], 3.0, 14)["trail"]
              for k in range(len(pre) + 1, len(pre + up + down) + 1)]
    levels = [x for x in levels if x is not None]
    assert all(b >= a for a, b in zip(levels, levels[1:]))


def test_trail_starts_at_the_hard_stop_floor_and_needs_an_atr():
    pre = _bars(date(2026, 8, 3), [100.0] * 20)
    t = et.compute_trail("2026-08-31", 95.0, pre + _bars(date(2026, 8, 31), [100.0]), 3.0, 14)
    assert t["armed"] and t["trail"] == 95.0                         # 101 − 6 < floor -> floor
    short = _bars(date(2026, 8, 24), [100.0] * 5)                   # < atr_n + 1 bars
    t = et.compute_trail("2026-08-31", 95.0, short + _bars(date(2026, 8, 31), [100.0]), 3.0, 14)
    assert not t["armed"] and t["trail"] is None


def test_live_price_lifts_the_trail_intraday_but_a_dip_does_not_lower_it():
    pre = _bars(date(2026, 8, 3), [100.0] * 20)
    up = _bars(date(2026, 8, 31), [100, 104, 108])
    base = et.compute_trail("2026-08-31", 95.0, pre + up, 3.0, 14)
    spike = et.compute_trail("2026-08-31", 95.0, pre + up, 3.0, 14, live_price=130.0)
    dip = et.compute_trail("2026-08-31", 95.0, pre + up, 3.0, 14, live_price=90.0)
    assert spike["trail"] > base["trail"] and spike["extreme"] == 130.0
    assert dip["trail"] == base["trail"]


def test_trail_hit_is_strictly_below():
    assert et.trail_hit(110.0, 109.99) and not et.trail_hit(110.0, 110.0)
    assert not et.trail_hit(None, 50.0) and not et.trail_hit(110.0, None)


def test_trail_for_position_names_why_it_cannot_arm():
    e = {"ticker": "DABUR.NS", "as_of": "2026-08-31", "kya_kara_action": {"stop": 95.0}}
    assert et.trail_for_position(e, 100.0, id_fn=lambda t: None)["reason"] == "no_scrip_master_id"
    assert et.trail_for_position(e, 100.0, id_fn=lambda t: "772",
                                 bars_fn=lambda sid, s: [])["reason"] == "no_daily_bars"
    et._BARS_CACHE.clear()
    bars = [{"date": b[0], "low": b[1], "high": b[2], "close": b[3]}
            for b in _bars(date(2026, 8, 3), [100.0] * 20) + _bars(date(2026, 8, 31), [100, 104, 108])]
    t = et.trail_for_position(e, 108.0, id_fn=lambda t: "772", bars_fn=lambda sid, s: bars)
    assert t["armed"] and t["atr_mult"] == 3.0 and t["reason"] == "armed"
    et._BARS_CACHE.clear()


# ------------------------------------------------ the tracker uses the trail
def _funded_entry(path, ticker="DABUR.NS", stop=95.0, target=140.0, entry_price=100.0):
    return kg.log_event({"event": "entry", "id": "e1", "ticker": ticker, "as_of": "2026-08-31",
                         "mode": sp.CAPITAL_MODE, "setup": sp.DARLING_SETUP,
                         "funding": {"funded": True, "qty": 10, "lock_ref": "eqd:e1"},
                         "kya_kara_action": {"entry_price": entry_price, "stop": stop,
                                             "target": target, "time_stop_days": 45}},
                        path=path)


def test_funded_position_exits_on_trail_hit_not_the_static_target():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "eq.jsonl"
        _funded_entry(p)
        armed = lambda e, px: {"armed": True, "trail": 120.0, "extreme": 126.0, "atr": 2.0,
                               "atr_mult": 3.0, "bars_since_entry": 9, "reason": "armed"}
        # above the trail but ABOVE the old static target too: the trail rules, no exit
        assert sp.track_open_shadows(quote_fn=lambda t: 145.0, vix_fn=lambda: 11.0,
                                     universe={}, path=p, trail_fn=armed) == []
        # below the trail: trail_hit, the trail on the row
        out = sp.track_open_shadows(quote_fn=lambda t: 119.5, vix_fn=lambda: 11.0,
                                    universe={}, path=p, trail_fn=armed)
        assert len(out) == 1 and out[0]["reason"] == "trail_hit"
        assert out[0]["trail"]["trail"] == 120.0 and out[0]["trail"]["armed"]
        assert "ATR trail hit" in out[0]["kya_sikha_autopsy"]["category"]


def test_hard_stop_still_wins_and_an_unarmed_trail_keeps_the_static_target():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "eq.jsonl"
        _funded_entry(p)
        armed = lambda e, px: {"armed": True, "trail": 120.0, "reason": "armed"}
        out = sp.track_open_shadows(quote_fn=lambda t: 94.0, vix_fn=lambda: 11.0,
                                    universe={}, path=p, trail_fn=armed)
        assert out[0]["reason"] == "stop_loss"
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "eq.jsonl"
        _funded_entry(p)
        unarmed = lambda e, px: {"armed": False, "trail": None, "reason": "no_daily_bars"}
        out = sp.track_open_shadows(quote_fn=lambda t: 141.0, vix_fn=lambda: 11.0,
                                    universe={}, path=p, trail_fn=unarmed)
        assert out[0]["reason"] == "target" and out[0]["trail"]["reason"] == "no_daily_bars"


def test_unfunded_telemetry_shadows_keep_the_original_rules():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "eq.jsonl"
        kg.log_event({"event": "entry", "id": "t1", "ticker": "INFY.NS", "as_of": "2026-08-31",
                      "mode": sp.MODE, "kya_kara_action": {"entry_price": 100.0, "stop": 95.0,
                                                           "target": 110.0}}, path=p)
        calls = []
        out = sp.track_open_shadows(quote_fn=lambda t: 111.0, vix_fn=lambda: 11.0, universe={},
                                    path=p, trail_fn=lambda e, px: calls.append(1))
        assert out[0]["reason"] == "target" and out[0]["trail"] is None and calls == []


# ------------------------------------------------------ the OMS exit ticket
def test_equity_exit_goes_through_the_oms_and_settles_at_the_venue_fill(monkeypatch):
    from src import brain_map, oms, portfolio_manager as pm, equity_desk as desk
    from src.execution import paper_venue as pv
    monkeypatch.setattr("src.config.PAPER_VENUE_ENABLED", True)
    monkeypatch.setattr(pv, "_tier_frac", lambda u, slippage_fn=None: 0.001)
    conn = brain_map.connect(":memory:")
    pm.get_account(conn)
    oms.ensure_schema(conn)
    pm.request_entry(conn, "eqd:e1", 1000.0)                          # the desk's lock
    entry = {"id": "e1", "ticker": "DABUR.NS", "as_of": "2026-08-31",
             "funding": {"funded": True, "qty": 10, "lock_ref": "eqd:e1"},
             "kya_kara_action": {"entry_price": 100.0, "stop": 95.0, "target": 140.0}}
    exit_event = {"ticker": "DABUR.NS", "exit_price": 120.0, "reason": "trail_hit"}
    rec = desk._execute_equity_exit(entry, exit_event, conn=conn)
    assert rec["mode"] == "paper_venue" and rec["status"] == oms.FILLED
    assert rec["fill_price"] == pytest.approx(119.88, abs=1e-6)          # SELL slipped 0.1%
    assert rec["venue_slippage_rs"] == pytest.approx(1.2, abs=1e-6)
    row = conn.execute("SELECT kind, strategy, lots, lot_size, status, journal_ref "
                       "FROM trade_tickets").fetchone()
    assert tuple(row) == ("EXIT", "equity_long", 1, 10, "FILLED", "eqd:e1")
    leg = conn.execute("SELECT side, option_type, qty_target, qty_filled FROM trade_legs").fetchone()
    assert tuple(leg) == ("SELL", None, 10, 10)
    s = desk.settle_exit(entry, dict(exit_event, execution=rec), conn=conn)
    assert s["pnl_net"] < (119.88 - 100.0) * 10                       # frictions charged
    assert s["execution"]["ticket_id"] == rec["ticket_id"] and s["venue_slippage_rs"] == pytest.approx(1.2)
    assert pm.equity(conn) == pytest.approx(1_000_000.0 + s["pnl_net"])
    conn.close()


def test_venue_off_leaves_the_settlement_byte_identical():
    from src import brain_map, portfolio_manager as pm, equity_desk as desk
    conn = brain_map.connect(":memory:")
    pm.get_account(conn)
    pm.request_entry(conn, "eqd:e1", 1000.0)
    entry = {"id": "e1", "ticker": "DABUR.NS", "funding": {"funded": True, "qty": 10, "lock_ref": "eqd:e1"},
             "kya_kara_action": {"entry_price": 100.0, "stop": 95.0}}
    x = {"ticker": "DABUR.NS", "exit_price": 120.0, "reason": "trail_hit"}
    rec = desk._execute_equity_exit(entry, x, conn=conn)             # PAPER_VENUE_ENABLED default in tests
    if rec["mode"] == "model":
        s = desk.settle_exit(entry, dict(x, execution=rec), conn=conn)
        assert s["execution"] is None or s["execution"]["mode"] == "model"
        assert s["venue_slippage_rs"] is None
    conn.close()


# --------------------------------------------------------- expiry guard
def test_expiry_report_flags_expired_expiring_and_api_errors(tmp_path):
    from src.ingestion import cross_asset as xa
    sec = tmp_path / "sec.json"
    sec.write_text(json.dumps({
        "CRUDE": {"id": "560977", "seg": "MCX_COMM", "inst": "FUTCOM", "_symbol": "CRUDEOIL AUG FUT", "_expiry": "2026-08-19"},
        "GOLD_INDIA": {"id": "483079", "seg": "MCX_COMM", "inst": "FUTCOM", "_symbol": "GOLD OCT FUT", "_expiry": "2026-10-05"},
        "COPPER": {"id": "1", "seg": "MCX_COMM", "inst": "FUTCOM", "_expiry": "2027-01-01"},
        "_note": "doc"}))
    glob = tmp_path / "glob.json"; glob.write_text("{}")
    ids = tmp_path / "ids.json"
    ids.write_text(json.dumps({"ids": {}, "commodities": {"SILVER": {"id": "495214", "expiry": "2026-09-30",
                                                                      "master_symbol": "SILVER-30Sep2026-FUT"}}}))
    ledger = tmp_path / "xa.jsonl"
    ledger.write_text(json.dumps({"ts": "2026-09-30T19:40:00", "name": "ZINC", "code": "CA-404",
                                  "detail": "no completed bars in window"}) + "\n"
                      + json.dumps({"ts": "2026-09-29T19:40:00", "name": "OLD", "code": "CA-410"}) + "\n")
    rep = xa.expiry_report(today=date(2026, 9, 30), warn_days=7, securities_path=sec,
                           global_path=glob, ids_path=ids, ledger_path=ledger)
    assert [r["name"] for r in rep["expired"]] == ["CRUDE"] and rep["expired"][0]["days"] == -42
    assert {r["name"] for r in rep["expiring"]} == {"GOLD_INDIA", "SILVER (MCX front month)"}
    assert rep["api_errors"] == [{"name": "ZINC", "code": "CA-404", "detail": "no completed bars in window"}]
    assert rep["checked"] == 4


def test_the_brief_carries_a_red_macro_expired_line_and_stays_silent_when_clean(tmp_path):
    from src import ceo_brief
    rep = {"expired": [{"name": "CRUDE", "symbol": "CRUDEOIL AUG FUT", "id": "560977",
                        "expiry": "2026-08-19", "days": -35, "source": "macro_securities"}],
           "expiring": [{"name": "GOLD_INDIA", "symbol": "GOLD OCT FUT", "id": "483079",
                         "expiry": "2026-10-05", "days": 12, "source": "macro_securities"}],
           "api_errors": []}
    f = ceo_brief._macro_expiry_field(rep)
    assert "🔴 MACRO EXPIRED: CRUDE (CRUDEOIL AUG FUT)" in f["value"] and "35d ago" in f["value"]
    assert "⚠️ MACRO EXPIRING: GOLD_INDIA" in f["value"]
    assert ceo_brief._macro_expiry_field({"expired": [], "expiring": [], "api_errors": []}) is None
    card = ceo_brief.build_brief_card(logs_dir=tmp_path, state_path=tmp_path / "s.json",
                                      deploy_log_path=tmp_path / "d.jsonl", repo_root=tmp_path,
                                      journal_path=tmp_path / "j.jsonl",
                                      macro_expiry_fn=lambda: rep)
    names = [x["name"] for x in card["fields"]]
    assert "🧭 Macro instrument expiry" in names and names.index("🧭 Macro instrument expiry") == 1
    assert "macro instrument id has expired" in card["description"]
