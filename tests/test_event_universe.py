"""
The event book's widened option universe (decision #191, owner ruling 2026-11-23 "widen the lists").
Exchange lot sizes from the F&O bhavcopy, the events-only resolver, the stock-option exit window for
extension names, the node's pre-LLM filter and the id build. Offline: injected files and chains only.

    python -m pytest tests/test_event_universe.py -q
"""
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, portfolio_manager as pm  # noqa: E402
from src.event_engine import IST, TAG_LLM, detect, executor, settler, store, universe  # noqa: E402
from src.ingestion import fo_bhavcopy as FO, scrip_master as SM  # noqa: E402

CFG = dict(config.EVENT_ENGINE_DEFAULTS, execute_enabled=True)
TODAY = date.today()
EXP_NEAR = (TODAY + timedelta(days=35)).isoformat()
EXP_FAR = (TODAY + timedelta(days=63)).isoformat()
NOT_NODE = lambda: False  # noqa: E731


def _fo(as_of=None, **over):
    sym = {"EVTSTOCK": {"tier": "tier1", "rank": 3, "lots": {EXP_NEAR: 150, EXP_FAR: 175}},
           "EVTTWO": {"tier": "tier2", "rank": 40, "lots": {EXP_NEAR: 600}},
           "EVTTHIN": {"tier": "illiquid", "rank": 120, "lots": {EXP_NEAR: 900}},
           "EVTBAN": {"tier": "banned", "rank": 9},
           "TCS": {"tier": "tier1", "rank": 5, "lots": {EXP_FAR: 250}}}
    sym.update(over)
    return {"as_of": (as_of or TODAY).isoformat(), "banned": ["EVTBAN"], "symbols": sym}


IDS = {"EVTSTOCK": {"id": "99001", "series": "EQ"}, "EVTTWO": {"id": "99002", "series": "EQ"},
       "EVTTHIN": {"id": "99003", "series": "EQ"}}

FO_CSV = """INSTRUMENT,SYMBOL    ,EXP_DATE  ,OPEN_PRICE ,HI_PRICE   ,LO_PRICE   ,CLOSE_PRICE,OPEN_INT*      ,TRD_VAL           ,TRD_QTY          ,NO_OF_CONT       ,NO_OF_TRADE
FUTSTK    ,HDFCBANK  ,29/12/2026,00000990.00,00000999.00,00000985.00,00000995.00,000000100000000,     9000000000.00,         13000000,            20000,            9000
FUTSTK    ,HDFCBANK  ,25/01/2027,00000995.00,00001000.00,00000990.00,00000998.00,000000000500000,      500000000.00,           150000,              200,             100
FUTSTK    ,ODDLOT    ,29/12/2026,00000100.00,00000101.00,00000099.00,00000100.00,000000000100000,         1000000.00,             1001,                2,               2
FUTSTK    ,NOTRADE   ,29/12/2026,00000100.00,00000101.00,00000099.00,00000100.00,000000000100000,               0.00,                0,                0,               0
OPTSTK    ,CLASH     ,29/12/2026,00000005.00,00000006.00,00000004.00,00000005.00,000000000100000,         1000000.00,             1000,                2,               2
FUTSTK    ,CLASH     ,29/12/2026,00000100.00,00000101.00,00000099.00,00000100.00,000000000100000,         1000000.00,             1200,                2,               2
FUTIDX    ,NIFTY     ,29/12/2026,00026000.00,00026100.00,00025900.00,00026050.00,000000010000000,    99000000000.00,          6500000,           100000,           50000
"""


# ------------------------------------------------------------------ exchange lot sizes
def test_lot_size_is_traded_quantity_over_contracts_per_expiry():
    lots = FO.parse_lot_sizes(FO_CSV)
    assert lots["HDFCBANK"] == {"2026-12-29": 650, "2027-01-25": 750}      # the Jan-2027 revision shows
    assert "ODDLOT" not in lots and "NOTRADE" not in lots                  # inexact / nothing traded
    assert "CLASH" not in lots                                             # rows disagree: not trusted
    assert "NIFTY" not in lots                                             # index rows stay out


def test_liquidity_snapshot_carries_the_lots(tmp_path):
    d = tmp_path / "2026-11-20"
    d.mkdir()
    (d / "fo.csv").write_text(FO_CSV)
    s = FO.liquidity_snapshot(lake_dir=tmp_path, write=False)
    assert s["symbols"]["HDFCBANK"]["lots"]["2027-01-25"] == 750
    assert "lots" not in s["symbols"]["NOTRADE"]


# ------------------------------------------------------------------ the resolver
def test_core_names_resolve_unchanged_and_need_no_file():
    idx = universe.resolve("BANKNIFTY", CFG, TODAY, fo={}, ids={})
    assert idx["ok"] and idx["underlying"] == "NIFTY BANK" and idx["lot"] == 30 and not idx["stock"]
    core = universe.resolve("RELIANCE", CFG, TODAY, fo={}, ids={})
    assert core["ok"] and core["underlying"] == "RELIANCE.NS" and core["lot"] == 500 and core["kind"] == "core"


def test_core_stock_takes_the_exchange_lot_when_the_contract_has_one():
    meta = universe.resolve("TCS", CFG, TODAY, fo=_fo(), ids={})
    assert universe.lot_for(meta, EXP_FAR) == (250, None)                  # exchange revision wins
    assert universe.lot_for(meta, EXP_NEAR) == (225, None)                 # else the hard-coded lot


def test_extension_stock_resolves_with_id_and_exchange_lots():
    meta = universe.resolve("EVTSTOCK.NS", CFG, TODAY, fo=_fo(), ids=IDS)
    assert meta["ok"] and meta["kind"] == "extension" and meta["underlying"] == "EVTSTOCK.NS"
    assert meta["security_id"] == "99001"
    assert universe.lot_for(meta, EXP_FAR) == (175, None)
    assert universe.lot_for(meta, (TODAY + timedelta(days=90)).isoformat())[0] is None


def test_extension_refusals_are_named():
    fo = _fo()
    assert "ban list" in universe.resolve("EVTBAN", CFG, TODAY, fo=fo, ids=IDS)["reason"]
    assert "outside event_engine.universe_tiers" in universe.resolve("EVTTHIN", CFG, TODAY, fo=fo, ids=IDS)["reason"]
    assert "no option contract" in universe.resolve("SOLEX", CFG, TODAY, fo=fo, ids=IDS)["reason"]
    stale = universe.resolve("EVTSTOCK", CFG, TODAY, fo=_fo(as_of=TODAY - timedelta(days=6)), ids=IDS)
    assert not stale["ok"] and "6 days old" in stale["reason"]
    no_id = universe.resolve("EVTTWO", CFG, TODAY, fo=fo, ids={})
    assert not no_id["ok"] and "no Dhan security id" in no_id["reason"]
    wide = dict(CFG, universe_tiers=["tier1", "tier2", "illiquid"])
    assert universe.resolve("EVTTHIN", wide, TODAY, fo=fo, ids=IDS)["ok"]


def test_tip_desk_door_stays_core_only():
    assert executor.instrument("INDIGO")[0] is None
    assert executor.instrument("RELIANCE") == ("RELIANCE.NS", 500)


# ------------------------------------------------------------------ settlement risk
def test_extension_stock_gets_the_stock_floor_and_seven_day_exit():
    from src.options_proposer import EQUITY_MIN_DAYS_TO_EXPIRY, MIN_DAYS_TO_EXPIRY
    assert universe.floor_days("EVTSTOCK.NS") == EQUITY_MIN_DAYS_TO_EXPIRY
    assert universe.floor_days("NIFTY 50") == MIN_DAYS_TO_EXPIRY
    monday = date(2026, 12, 7)                                             # a plain week, no holiday
    expiry = monday + timedelta(days=6)
    assert universe.in_forced_exit_window("EVTSTOCK.NS", expiry, monday)
    assert not universe.in_forced_exit_window("NIFTY 50", monday + timedelta(days=6), monday)


# ------------------------------------------------------------------ executor + settler end to end
def _chain(spot=100.0, ask=1.0, bid=0.9):
    oc = {f"{float(k):.6f}": {"ce": {"top_bid_price": bid, "top_ask_price": ask, "last_price": (bid + ask) / 2},
                              "pe": {"top_bid_price": bid, "top_ask_price": ask, "last_price": (bid + ask) / 2}}
          for k in (94, 96, 98, 100, 102, 104, 106, 108)}
    return {"last_price": spot, "oc": oc}


def _card(tmp_path, ticker):
    c = {"schema": "event_card/1.1", "event_id": "pli-evt000001",
         "generated_at": datetime.now(IST).isoformat(timespec="seconds"), "source": {"title": "PLI"},
         "suggestions": [{"ticker": ticker, "direction": "bullish", "instrument": "long_otm_call",
                          "horizon_days": 30,
                          "strike_paths": {"fixed": {"otm_pct": 3.0},
                                           "llm": {"price_response_pct": [2, 6, 9], "source": TAG_LLM}},
                          "max_loss_per_trade_rs": 5000,
                          "basis": {"materiality_pct": [3, 5, 7], "materiality_source": TAG_LLM,
                                    "priced_in": "underreaction"}}]}
    d = tmp_path / "cards"
    d.mkdir(exist_ok=True)
    (d / f"event_card__{c['event_id']}.json").write_text(json.dumps(c))
    return ((d, "event_card__*.json"),)


def _patch_files(monkeypatch, fo=None):
    monkeypatch.setattr(universe, "load_fo", lambda path=None: _fo() if fo is None else fo)
    monkeypatch.setattr(universe, "load_ids", lambda path=None: IDS)


def test_executor_opens_an_extension_stock_at_the_exchange_lot(tmp_path, monkeypatch):
    _patch_files(monkeypatch)
    conn = sqlite3.connect(tmp_path / "brain.db")
    pm.ensure_accounts_schema(conn)
    expiries = lambda u: [(TODAY + timedelta(days=n)).isoformat() for n in (3, 10, 35, 63)]  # noqa: E731
    asked = []
    r = executor.run(conn=conn, dirs=_card(tmp_path, "EVTSTOCK"), cfg=CFG, force_hours=True,
                     expiries_fn=lambda u: asked.append(u) or expiries(u),
                     chain_fn=lambda u, e: _chain(), notify_fn=lambda m: None, node_check=NOT_NODE)
    opened = [x for x in r["results"] if x["action"] == "opened"]
    assert asked == ["EVTSTOCK.NS"] and len(opened) == 2
    pos = store.open_positions(conn)
    assert {p["underlying"] for p in pos} == {"EVTSTOCK.NS"} and {p["lot_size"] for p in pos} == {150}
    assert {p["expiry"] for p in pos} == {EXP_NEAR}
    assert all(p["entry_total_rs"] <= 5000 for p in pos)
    # the settler holds it under the stock window: 6 days before expiry it must be out
    near = date.fromisoformat(EXP_NEAR)
    assert settler.decide(pos[0], 0.95, near - timedelta(days=6), CFG) == "pre_expiry"


def test_executor_abstains_when_the_expiry_has_no_exchange_lot(tmp_path, monkeypatch):
    fo = _fo(EVTSTOCK={"tier": "tier1", "rank": 3, "lots": {EXP_FAR: 175}})
    _patch_files(monkeypatch, fo)
    conn = sqlite3.connect(tmp_path / "brain.db")
    pm.ensure_accounts_schema(conn)
    expiries = lambda u: [EXP_NEAR, EXP_FAR]  # noqa: E731
    r = executor.run(conn=conn, dirs=_card(tmp_path, "EVTSTOCK"), cfg=CFG, force_hours=True,
                     expiries_fn=expiries, chain_fn=lambda u, e: _chain(), notify_fn=lambda m: None,
                     node_check=NOT_NODE)
    assert [x["reason"] for x in r["results"]] == [f"no exchange lot size for EVTSTOCK.NS {EXP_NEAR}"]
    assert store.open_positions(conn) == []


def test_executor_without_the_snapshot_keeps_the_old_answer(tmp_path, monkeypatch):
    _patch_files(monkeypatch, {})
    conn = sqlite3.connect(tmp_path / "brain.db")
    pm.ensure_accounts_schema(conn)
    r = executor.run(conn=conn, dirs=_card(tmp_path, "INDIGO"), cfg=CFG, force_hours=True,
                     expiries_fn=lambda u: [], chain_fn=lambda u, e: None, notify_fn=lambda m: None,
                     node_check=NOT_NODE)
    assert r["results"][0]["reason"] == "INDIGO has no option contract in the VM's option universe"


# ------------------------------------------------------------------ node filter + id build
def test_node_filter_matches_extension_names_but_not_word_symbols(tmp_path):
    companies = tmp_path / "companies.json"
    companies.write_text(json.dumps({"companies": {
        "EVTSTOCK": ["Event Stock Industries", "Event Stock Industries Limited"],
        "IDEA": ["Vodafone Idea", "Vodafone Idea Limited"]}}))
    fo = _fo(IDEA={"tier": "tier2", "rank": 30})
    pat = detect._universe_pattern(CFG, fo, companies)
    assert pat.search("Event Stock Industries wins a PLI award")
    assert pat.search("EVTSTOCK shares jump")
    assert pat.search("Vodafone Idea gets AGR relief")
    assert not pat.search("a bright idea for the weekend")
    assert not pat.search("EVTTHIN posts results")                         # illiquid tier not configured


def test_id_build_wants_the_event_tiers(tmp_path):
    p = tmp_path / "fo.json"
    p.write_text(json.dumps(_fo()))
    assert SM._event_fo_symbols(p) == {"EVTSTOCK", "EVTTWO", "TCS"}
    assert SM._event_fo_symbols(tmp_path / "missing.json") == set()
