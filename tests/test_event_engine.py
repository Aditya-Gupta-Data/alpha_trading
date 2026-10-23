"""
Event engine 1.1 — decision #156 (Architect rulings 2026-10-21; method docs/EVENT_PLAYBOOK.md).

Node half (emit): detect → exposure (LLM, source-tagged) → materiality and priced-in (our arithmetic)
→ card → ship. VM half (executor, settler): auto-executed long OTM options on the ISOLATED
PAPER_EVENTS book, ₹5,000 max loss a trade, both A/B strike paths, money only through the
paper-account door. Offline: fake LLM, fake chains, tmp databases; no network, no real data/ or logs/.

    python -m pytest tests/test_event_engine.py -q
"""
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, node_role, portfolio_manager as pm  # noqa: E402
from src.event_engine import (IST, TAG_COMPUTED, TAG_LLM, TAG_REPORTED, card as card_mod,  # noqa: E402
                              detect, emit, executor, exposure, materiality, priced_in, settler, store,
                              strikes, worst_tag)
from src.event_engine.llm import EventLLM  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CFG = dict(config.EVENT_ENGINE_DEFAULTS, execute_enabled=True)

POLICY_TEXT = (
    "India and the EU concluded the free trade agreement on 27 January 2026. "
    "In-quota duty on cars above EUR 35,000 falls from 110% to 30% in Year 1 and to 10% by Year 5. "
    "The car quota is 1 lakh units in Year 1. Cars likely to retail under Rs 25 lakh will not be exported. "
) * 2


# ------------------------------------------------------------------ fakes
class FakeExtractor:
    """Answers by prompt: the classify prompt and the exposure prompt."""
    def __init__(self, classify=None, expose=None, fail=False):
        self.classify, self.expose, self.fail, self.calls = classify, expose, fail, 0

    def chat_json(self, system, user):
        self.calls += 1
        if self.fail:
            raise RuntimeError("router down")
        return self.classify if system == detect.CLASSIFY_PROMPT else self.expose


CLASSIFIED = {"is_policy_event": True, "type": "trade_deal", "issuer": "GoI", "stage": "concluded",
              "title": "India-EU FTA", "announced_on": "2026-01-27", "key_dates": {"concluded": "2026-01-27"}}
EXPOSED = {
    "mechanics": [
        {"rule": "in-quota duty, CBU cars above EUR 35k", "before": 110, "after": 30, "unit": "%",
         "quote": "In-quota duty on cars above EUR 35,000 falls from 110% to 30% in Year 1 and to 10% by Year 5."},
        {"rule": "invented", "before": 1, "after": 2, "unit": "%", "quote": "a sentence that is not in the text"}],
    "exposures": [
        {"ticker": "NIFTY", "segment": "index proxy", "direction": "positive", "route": "import",
         "revenue_effect_cr": [300, 500, 700], "annual_revenue_cr": 10000, "price_response_pct": [2, 4, 6],
         "horizon_days": 30, "reasoning": "test"},
        {"ticker": "M&M", "segment": "mass market", "direction": "none", "route": "local",
         "revenue_effect_cr": None, "annual_revenue_cr": 150000, "price_response_pct": None},
        {"ticker": "TMPV.NS", "segment": "JLR", "direction": "positive", "route": "import",
         "revenue_effect_cr": [1000, 2800, 4000], "annual_revenue_cr": None, "price_response_pct": [0, 1, 2]}],
}


def _llm(tmp_path, **kw):
    return EventLLM(extractor=FakeExtractor(**kw), cache_dir=tmp_path / "cache", ledger=tmp_path / "calls.jsonl",
                    cap=40)


def _bars(closes_by_session):
    return [{"session": s, "close": c} for s, c in closes_by_session]


FLAT_BARS = _bars([("2026-01-23", 100.0), ("2026-01-26", 100.0), ("2026-01-27", 100.0),
                   ("2026-01-28", 100.0), ("2026-01-29", 100.0), ("2026-01-30", 100.0)])


def _chain(spot=100.0, strikes_list=(94, 96, 98, 100, 102, 104, 106, 108, 110), ask=1.0, bid=0.9):
    oc = {}
    for k in strikes_list:
        q = {"top_bid_price": bid, "top_ask_price": ask, "last_price": (bid + ask) / 2}
        oc[f"{float(k):.6f}"] = {"ce": dict(q), "pe": dict(q)}
    return {"last_price": spot, "oc": oc}


def _db(tmp_path):
    conn = sqlite3.connect(tmp_path / "brain.db")
    pm.ensure_accounts_schema(conn)
    return conn


def _card(tmp_path, generated_at=None, suggestions=None):
    c = {"schema": "event_card/1.1", "event_id": "trade_deal-abc123",
         "generated_at": generated_at or datetime.now(IST).isoformat(timespec="seconds"),
         "source": {"title": "India-EU FTA"},
         "suggestions": suggestions if suggestions is not None else [{
             "ticker": "NIFTY", "direction": "bullish", "instrument": "long_otm_call", "horizon_days": 30,
             "strike_paths": {"fixed": {"otm_pct": 3.0}, "llm": {"price_response_pct": [2, 6, 9], "source": TAG_LLM}},
             "max_loss_per_trade_rs": 5000,
             "basis": {"materiality_pct": [3, 5, 7], "materiality_source": TAG_LLM, "priced_in": "underreaction"}}]}
    d = tmp_path / "cards"
    d.mkdir(exist_ok=True)
    (d / f"event_card__{c['event_id']}.json").write_text(json.dumps(c))
    return c, ((d, "event_card__*.json"),)


EXPIRIES = lambda und: [(date.today() + timedelta(days=n)).isoformat() for n in (3, 10, 35, 63)]  # noqa: E731


# ------------------------------------------------------------------ config, account isolation
def test_config_discord_seven_and_the_event_block():
    cfg = json.loads((ROOT / "config.json").read_text())
    assert cfg["discord_daily_budget"] == 7
    ee = cfg["event_engine"]
    assert ee["max_loss_per_trade_rs"] == 5000 and ee["materiality_threshold_pct"] == 2.0
    assert cfg["paper_events_capital_rs"] == 1000000
    assert ee["execute_enabled"] is True             # flipped by the Architect 2026-10-23 (#158)


def test_paper_events_is_on_the_door_but_never_a_shadow_of_the_primary(monkeypatch):
    assert pm.ACCOUNT_PAPER_EVENTS == "PAPER_EVENTS" and pm.PAPER_ACCOUNTS["PAPER_EVENTS"] == 1_000_000
    for name in ("PAPER_2L_ACCOUNT_ENABLED", "CAPITAL_ROTATION_ENABLED", "PAPER_2L_LIVE_ACCOUNT_ENABLED",
                 "SHADOW_LEARNER_ENABLED"):
        monkeypatch.setattr(pm, name, True)
    ids = pm.shadow_account_ids()
    assert "PAPER_EVENTS" not in ids and "PAPER_2L" in ids and "PAPER_SHADOW_LEARNER" in ids


def test_the_vm_jobs_never_run_on_the_node_and_the_emitter_does():
    assert node_role.never_on_node("python -m src.event_engine.executor")
    assert node_role.never_on_node("python -m src.event_engine.settler")
    assert not node_role.never_on_node("python -m src.event_engine.emit")
    vm = (ROOT / "scripts" / "setup_cron.sh").read_text()
    assert "-m src.event_engine.executor" in vm and "-m src.event_engine.settler" in vm
    node = (ROOT / "scripts" / "setup_node_replica_cron.sh").read_text()
    assert "-m src.event_engine.emit" in node


def test_worst_tag():
    assert worst_tag(TAG_REPORTED, TAG_REPORTED) == TAG_REPORTED
    assert worst_tag(TAG_REPORTED, TAG_COMPUTED) == TAG_COMPUTED
    assert worst_tag(TAG_COMPUTED, TAG_LLM, TAG_REPORTED) == TAG_LLM
    assert worst_tag() == TAG_COMPUTED


# ------------------------------------------------------------------ the LLM door
def test_llm_caches_caps_and_never_raises(tmp_path):
    fx = FakeExtractor(classify={"a": 1})
    llm = EventLLM(extractor=fx, cache_dir=tmp_path / "c", ledger=tmp_path / "l.jsonl", cap=1)
    assert llm.ask(detect.CLASSIFY_PROMPT, "x") == {"a": 1}
    assert llm.ask(detect.CLASSIFY_PROMPT, "x") == {"a": 1} and fx.calls == 1      # cached
    assert llm.ask(detect.CLASSIFY_PROMPT, "y") is None and "cap" in llm.last_skip   # cap 1 spent
    down = EventLLM(extractor=FakeExtractor(fail=True), cache_dir=tmp_path / "d", ledger=tmp_path / "m.jsonl")
    assert down.ask("s", "u") is None and "router down" in down.last_skip


# ------------------------------------------------------------------ detect (gate 1)
def test_classify_gates(tmp_path):
    short = detect.classify({"title": "t", "text": "headline only"}, _llm(tmp_path, classify=CLASSIFIED))
    assert not short["ok"] and "gate 1" in short["reason"]
    ok = detect.classify({"title": "t", "text": POLICY_TEXT}, _llm(tmp_path, classify=CLASSIFIED))
    assert ok["ok"] and ok["event"]["type"] == "trade_deal" and ok["event"]["stage"] == "concluded"
    rumour = detect.classify({"title": "t", "text": POLICY_TEXT},
                             _llm(tmp_path / "r", classify=dict(CLASSIFIED, stage="rumour")))
    assert not rumour["ok"] and "rumour" in rumour["reason"]
    notpol = detect.classify({"title": "t", "text": POLICY_TEXT},
                             _llm(tmp_path / "n", classify=dict(CLASSIFIED, is_policy_event=False)))
    assert not notpol["ok"] and notpol["reason"] == "not a policy event"


def test_inbox_and_seen_ledger(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "fta.md").write_text("# India-EU FTA\n\n" + POLICY_TEXT)
    (inbox / "x.json").write_text(json.dumps({"title": "RBI", "text": "rate cut", "url": "https://rbi"}))
    items = detect.inbox_items(inbox)
    assert [i["title"] for i in items] == ["India-EU FTA", "RBI"]
    led = detect.SeenLedger(tmp_path / "seen.txt")
    led.add("k1")
    assert "k1" in detect.SeenLedger(tmp_path / "seen.txt")


# ------------------------------------------------------------------ exposure: provenance
def test_exposure_tags_every_number_by_source(tmp_path):
    ex = exposure.extract({"text": POLICY_TEXT}, _llm(tmp_path, expose=EXPOSED),
                          financials={"TMPV": {"annual_revenue_cr": 350000, "source": "FY26 annual report"}})
    m = {x["rule"]: x for x in ex["mechanics"]}
    assert m["in-quota duty, CBU cars above EUR 35k"]["source"] == TAG_REPORTED     # quote found in the text
    assert m["invented"]["source"] == TAG_LLM and any("quote not found" in f for f in ex["flags"])
    e = {x["ticker"]: x for x in ex["exposures"]}
    assert set(e) == {"NIFTY", "M&M", "TMPV"}                                       # .NS stripped
    assert e["NIFTY"]["revenue_effect_source"] == TAG_LLM and e["NIFTY"]["annual_revenue"]["source"] == TAG_LLM
    assert e["TMPV"]["annual_revenue"] == {"value": 350000.0, "source": TAG_REPORTED, "ref": "FY26 annual report"}
    assert e["M&M"]["direction"] == "none" and e["M&M"]["revenue_effect_cr"] is None


# ------------------------------------------------------------------ materiality (our arithmetic)
def test_materiality_gate():
    base = {"ticker": "X", "direction": "positive", "revenue_effect_source": TAG_LLM,
            "annual_revenue": {"value": 10000.0, "source": TAG_REPORTED}}
    passes = materiality.assess(dict(base, revenue_effect_cr=[300, 500, 700]), 2.0)
    assert passes["passes_gate"] and passes["effect_pct"] == [3.0, 5.0, 7.0] and passes["source"] == TAG_LLM
    small = materiality.assess(dict(base, revenue_effect_cr=[10, 50, 90]), 2.0)
    assert not small["passes_gate"] and "below" in small["reason"]
    straddle = materiality.assess(dict(base, revenue_effect_cr=[-300, 500, 700]), 2.0)
    assert not straddle["passes_gate"] and "straddles" in straddle["reason"]
    assert "no revenue-effect" in materiality.assess(dict(base, revenue_effect_cr=None), 2.0)["reason"]
    assert materiality.assess(dict(base, direction="none", revenue_effect_cr=[1, 2, 3]))["reason"] == "not exposed"
    computed = materiality.assess(dict(base, revenue_effect_cr=[300, 500, 700], revenue_effect_source=TAG_COMPUTED))
    assert computed["source"] == TAG_COMPUTED


# ------------------------------------------------------------------ priced-in
def test_priced_in_verdicts():
    mat = {"passes_gate": True, "effect_pct": [3.0, 5.0, 7.0], "source": TAG_LLM}
    up = _bars([("2026-01-26", 100.0), ("2026-01-27", 108.0), ("2026-01-28", 110.0), ("2026-01-29", 112.0),
                ("2026-01-30", 112.0)])
    down = _bars([("2026-01-26", 100.0), ("2026-01-27", 96.0), ("2026-01-28", 95.0), ("2026-01-29", 95.0),
                  ("2026-01-30", 95.0)])
    run = lambda bars, m=mat: priced_in.assess("X", m, "2026-01-27", bars_fn=lambda s: bars, cfg=CFG)  # noqa: E731
    over = run(up)
    assert over["verdict"] == "overreaction" and over["direction"] == "bearish" and over["event_move_pct"] == 12.0
    assert run(FLAT_BARS)["verdict"] == "underreaction" and run(FLAT_BARS)["direction"] == "bullish"
    opp = run(down)
    assert opp["verdict"] == "opposite" and opp["direction"] == "bullish"
    inline = run(_bars([("2026-01-26", 100.0), ("2026-01-27", 104.0), ("2026-01-28", 105.0),
                        ("2026-01-29", 105.0), ("2026-01-30", 105.5)]))
    assert inline["verdict"] == "in_line" and inline["direction"] is None
    assert run([])["verdict"] == "unknown"
    assert priced_in.assess("X", {"passes_gate": False}, "2026-01-27", bars_fn=lambda s: up)["reason"] == "not material"


# ------------------------------------------------------------------ strikes: the A/B paths
def test_strike_paths_sizing_and_expiry():
    ch = _chain()
    a = strikes.pick_fixed(ch, 100.0, "CE", 3.0)
    assert a["ok"] and a["strike"] == 104.0                    # first listed strike >= 103
    p = strikes.pick_fixed(ch, 100.0, "PE", 3.0)
    assert p["strike"] == 96.0                                 # first listed strike <= 97
    b = strikes.pick_llm(ch, 100.0, "CE", [2, 6, 9])
    assert b["ok"] and b["strike"] == 106.0
    near = strikes.pick_llm(ch, 100.0, "CE", [0, 0.2, 1])
    assert near["strike"] == 102.0                             # always strictly OTM
    assert not strikes.pick_llm(ch, 100.0, "CE", None)["ok"]
    assert not strikes.pick_fixed(ch, 100.0, "CE", 50.0)["ok"]
    assert strikes.size_lots(1.0, 65, 5000)["lots"] == 76      # 65 per lot -> 76 lots <= 5000
    s = strikes.size_lots(1.0, 65, 5000)
    assert s["premium_total_rs"] <= 5000
    over = strikes.size_lots(100.0, 65, 5000)
    assert over["lots"] == 0 and "6,500" in over["reason"]
    today = date(2026, 10, 21)
    exps = ["2026-10-23", "2026-10-28", "2026-11-25", "2026-12-30"]
    assert strikes.choose_expiry(exps, today, 30, 7) == "2026-11-25"
    assert strikes.choose_expiry(exps, today, 200, 7) == "2026-12-30"
    assert strikes.choose_expiry(["2026-10-23"], today, 30, 7) is None


# ------------------------------------------------------------------ the node half end to end
def test_emit_builds_a_card_and_ships_only_suggestions(tmp_path, monkeypatch):
    monkeypatch.setattr(emit, "STATE_DIR", tmp_path / "state")
    shipped = []
    cfg = dict(CFG)
    s = emit.run(items=[{"title": "India-EU FTA", "text": POLICY_TEXT, "url": "https://x", "source": "inbox:fta.md"}],
                 llm=_llm(tmp_path, classify=CLASSIFIED, expose=EXPOSED), bars_fn=lambda sym: FLAT_BARS,
                 card_dir=tmp_path / "cards", ship_fn=lambda p: shipped.append(p.name) or True,
                 seen=detect.SeenLedger(tmp_path / "seen.txt"), cfg=cfg)
    assert s["classified"] == 1 and len(s["cards"]) == 1 and s["shipped"] == 1
    card = json.loads(next((tmp_path / "cards").glob("*.json")).read_text())
    assert card["schema"] == "event_card/1.1" and card_mod.validate(card) == []
    tickers = [x["ticker"] for x in card["suggestions"]]
    assert tickers == ["NIFTY"]                     # TMPV: no revenue → not material; M&M: not exposed
    sug = card["suggestions"][0]
    assert sug["direction"] == "bullish" and sug["instrument"] == "long_otm_call"
    assert sug["max_loss_per_trade_rs"] == 5000 and sug["strike_paths"]["llm"]["source"] == TAG_LLM
    assert shipped and shipped[0].startswith("event_card__")
    again = emit.run(items=[{"title": "India-EU FTA", "text": POLICY_TEXT, "url": "https://x"}],
                     llm=_llm(tmp_path, classify=CLASSIFIED, expose=EXPOSED), bars_fn=lambda sym: FLAT_BARS,
                     card_dir=tmp_path / "cards", ship_fn=lambda p: True,
                     seen=detect.SeenLedger(tmp_path / "seen.txt"), cfg=cfg)
    assert again["items"] == 0                      # seen once, never re-spent


def test_emit_stops_without_marking_seen_when_the_llm_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr(emit, "STATE_DIR", tmp_path / "state")
    seen = detect.SeenLedger(tmp_path / "seen.txt")
    item = {"title": "t", "text": POLICY_TEXT}
    s = emit.run(items=[item], llm=_llm(tmp_path, fail=True), card_dir=tmp_path / "cards", do_ship=False,
                 seen=seen, cfg=CFG)
    assert s["stopped"] and "router down" in s["stopped"] and detect.item_id(item) not in seen
    off = emit.run(items=[item], llm=_llm(tmp_path), cfg=dict(CFG, emit_enabled=False))
    assert off["status"].startswith("disabled")


# ------------------------------------------------------------------ the VM half: executor
NOT_NODE = lambda: False  # noqa: E731


def test_executor_dry_run_plans_both_paths_and_writes_nothing(tmp_path):
    _, dirs = _card(tmp_path)
    conn = _db(tmp_path)
    r = executor.run(conn=conn, dirs=dirs, dry_run=True, cfg=CFG, expiries_fn=EXPIRIES,
                     chain_fn=lambda u, e: _chain(), node_check=NOT_NODE)
    planned = [x for x in r["results"] if x["action"] == "planned"]
    assert {x["path"] for x in planned} == {"fixed", "llm"}
    assert {x["strike"] for x in planned} == {104.0, 106.0}
    assert all(x["premium_total_rs"] <= 5000 and x["lots"] >= 1 for x in planned)
    store.ensure_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM event_positions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM paper_margin_locks").fetchone()[0] == 0


def test_executor_opens_both_paths_on_paper_events_only(tmp_path):
    _, dirs = _card(tmp_path)
    conn = _db(tmp_path)
    alerts = []
    primary_before = pm.account_summary(conn)
    r = executor.run(conn=conn, dirs=dirs, cfg=CFG, force_hours=True, expiries_fn=EXPIRIES,
                     chain_fn=lambda u, e: _chain(), notify_fn=alerts.append, node_check=NOT_NODE)
    opened = [x for x in r["results"] if x["action"] == "opened"]
    assert len(opened) == 2 and len(alerts) == 2 and all(a["event"] == "event_trade" for a in alerts)
    rows = conn.execute("SELECT account_id, margin_rs, lots FROM paper_margin_locks").fetchall()
    assert {r[0] for r in rows} == {"PAPER_EVENTS"} and all(r[1] <= 5000 for r in rows)
    pos = store.open_positions(conn)
    assert {p["path"] for p in pos} == {"fixed", "llm"} and all(p["underlying"] == "NIFTY 50" for p in pos)
    assert {p["response_source"] for p in pos} == {TAG_LLM}
    # the primary book is untouched
    assert pm.account_summary(conn)["locked_margin"] == primary_before["locked_margin"] == 0
    assert conn.execute("SELECT COUNT(*) FROM margin_locks").fetchone()[0] == 0
    # a card is executed once
    again = executor.run(conn=conn, dirs=dirs, cfg=CFG, force_hours=True, expiries_fn=EXPIRIES,
                         chain_fn=lambda u, e: _chain(), node_check=NOT_NODE)
    assert again["cards"] == 0


def test_executor_gates(tmp_path):
    conn = _db(tmp_path)
    _, dirs = _card(tmp_path)
    run = lambda **kw: executor.run(conn=conn, dirs=dirs, force_hours=True, expiries_fn=EXPIRIES,  # noqa: E731
                                    chain_fn=lambda u, e: _chain(), **kw)
    assert run(cfg=CFG, node_check=lambda: True)["status"].startswith("refused: analytics node")
    assert run(cfg=dict(CFG, execute_enabled=False), node_check=NOT_NODE)["status"].startswith("disabled")
    # one lot over the cap -> abstain, named
    dear = executor.run(conn=conn, dirs=dirs, cfg=CFG, force_hours=True, expiries_fn=EXPIRIES,
                        chain_fn=lambda u, e: _chain(ask=100.0, bid=99.0), node_check=NOT_NODE)
    assert {x["action"] for x in dear["results"]} == {"abstained"}
    assert any("cap" in (x["reason"] or "") for x in dear["results"])


def test_executor_rejects_old_cards_and_tickers_without_options(tmp_path):
    conn = _db(tmp_path)
    old = (datetime.now(IST) - timedelta(days=9)).isoformat(timespec="seconds")
    _, dirs = _card(tmp_path, generated_at=old)
    r = executor.run(conn=conn, dirs=dirs, cfg=CFG, force_hours=True, expiries_fn=EXPIRIES,
                     chain_fn=lambda u, e: _chain(), node_check=NOT_NODE)
    assert r["results"][0]["rejected"] and "days old" in r["results"][0]["rejected"][0]
    assert executor.instrument("M&M")[0] is None and "option universe" in executor.instrument("M&M")[1]
    assert executor.instrument("BANKNIFTY") == ("NIFTY BANK", 30)
    assert executor.instrument("RELIANCE") == ("RELIANCE.NS", 500)


def test_same_strike_opens_once(tmp_path):
    conn = _db(tmp_path)
    _, dirs = _card(tmp_path, suggestions=[{
        "ticker": "NIFTY", "direction": "bullish", "horizon_days": 30,
        "strike_paths": {"fixed": {"otm_pct": 3.0}, "llm": {"price_response_pct": [3, 4, 5]}}, "basis": {}}])
    r = executor.run(conn=conn, dirs=dirs, cfg=CFG, force_hours=True, expiries_fn=EXPIRIES,
                     chain_fn=lambda u, e: _chain(), node_check=NOT_NODE, notify_fn=lambda p: None)
    acts = {x["path"]: x["action"] for x in r["results"]}
    assert acts == {"fixed": "opened", "llm": "same_strike"} and len(store.open_positions(conn)) == 1


# ------------------------------------------------------------------ the VM half: settler
def _opened(tmp_path):
    conn = _db(tmp_path)
    _, dirs = _card(tmp_path)
    executor.run(conn=conn, dirs=dirs, cfg=CFG, force_hours=True, expiries_fn=EXPIRIES,
                 chain_fn=lambda u, e: _chain(), node_check=NOT_NODE, notify_fn=lambda p: None)
    return conn


def test_settler_take_profit_releases_through_the_door(tmp_path):
    conn = _opened(tmp_path)
    alerts = []
    r = settler.run(conn=conn, cfg=CFG, force_hours=True, chain_fn=lambda u, e: _chain(bid=5.5, ask=5.6),
                    forced_fn=lambda *a: False, notify_fn=alerts.append, node_check=NOT_NODE)
    assert len(r["closed"]) == 2 and all(c["reason"] == "take_profit" for c in r["closed"])
    total = sum(c["pnl"] for c in r["closed"])
    acct = pm.get_paper_account(conn, "PAPER_EVENTS")
    assert acct["realized_pnl"] == pytest.approx(total) and total > 0
    assert conn.execute("SELECT COUNT(*) FROM paper_margin_locks WHERE released_at IS NULL").fetchone()[0] == 0
    assert len(alerts) == 2 and store.open_positions(conn) == []


def test_settler_holds_marks_and_exits_on_rules(tmp_path):
    conn = _opened(tmp_path)
    held = settler.run(conn=conn, cfg=CFG, force_hours=True, chain_fn=lambda u, e: _chain(bid=1.2, ask=1.3),
                       forced_fn=lambda *a: False, notify_fn=lambda p: None, node_check=NOT_NODE)
    assert len(held["held"]) == 2 and not held["closed"]
    assert {p["last_mark"] for p in store.open_positions(conn)} == {1.2}
    pre = settler.run(conn=conn, cfg=CFG, force_hours=True, chain_fn=lambda u, e: _chain(bid=1.2, ask=1.3),
                      forced_fn=lambda *a: True, notify_fn=lambda p: None, node_check=NOT_NODE)
    assert {c["reason"] for c in pre["closed"]} == {"pre_expiry"}


def test_settler_expired_without_a_chain_loses_the_premium(tmp_path):
    conn = _opened(tmp_path)
    future = datetime.now(IST) + timedelta(days=120)

    def no_chain(u, e):
        raise RuntimeError("expired contract")
    r = settler.run(conn=conn, now=future, cfg=CFG, force_hours=True, chain_fn=no_chain,
                    notify_fn=lambda p: None, node_check=NOT_NODE)
    assert {c["reason"] for c in r["closed"]} == {"expired"}
    for c in r["closed"]:
        pos = conn.execute("SELECT entry_total_rs FROM event_positions WHERE journal_ref = ?",
                           (c["journal_ref"],)).fetchone()
        assert c["pnl"] == pytest.approx(-pos[0] - 2 * CFG["fees_per_order_rs"])
    assert settler.run(conn=conn, cfg=CFG, node_check=lambda: True)["status"].startswith("refused")


def test_decide_horizon_and_pnl_math():
    pos = {"expiry": "2026-12-30", "underlying": "NIFTY 50", "entry_premium": 2.0, "lot_size": 65, "lots": 10,
           "opened_at": "2026-10-01T09:35:00+05:30", "horizon_days": 30}
    assert settler.decide(pos, 2.5, date(2026, 10, 20), CFG, forced_fn=lambda *a: False) is None
    assert settler.decide(pos, 2.5, date(2026, 10, 31), CFG, forced_fn=lambda *a: False) == "horizon"
    assert settler.decide(pos, 10.0, date(2026, 10, 20), CFG, forced_fn=lambda *a: False) == "take_profit"
    assert settler.pnl_rs(pos, 3.0, 20) == pytest.approx((3.0 - 2.0) * 65 * 10 - 40)
