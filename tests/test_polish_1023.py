"""Final structural gaps (Architect 2026-10-23, #158): the announcement-date guard, the no-LLM
re-assessment of recent cards, index materiality on the price response, one learner entry per
(underlying, strategy, session), the executor switch and the fo_bhavcopy freshness read. Offline."""
import json
import sqlite3
from datetime import date, datetime, timedelta

from src import shadow_learner as sl, staleness_guard as sg
from src.event_engine import TAG_LLM, emit, materiality, priced_in
from tests.test_event_engine import CFG


# --------------------------------------------------------------- announcement date guard

def test_a_historical_llm_date_falls_back_to_the_publication_date():
    item = {"published": "Fri, 23 Oct 2026 06:41:46 +0530"}
    day, note = emit.announcement_date({"announced_on": "2025-02-12"}, item)      # the NAM-INDIA card
    assert day == "2026-10-23" and "historical reference" in note
    assert emit.announcement_date({"announced_on": "2026-10-19"}, item) == ("2026-10-19", None)   # 4 days: kept
    day, note = emit.announcement_date({"announced_on": None}, item)
    assert day == "2026-10-23" and "missing" in note


def test_an_inbox_text_without_a_publication_date_keeps_the_models_date():
    assert emit.announcement_date({"announced_on": "2026-01-27"}, {}) == ("2026-01-27", None)
    day, note = emit.announcement_date({"announced_on": None}, {}, today=date(2026, 10, 23))
    assert day == "2026-10-23" and "ingestion" in note


# --------------------------------------------------------------- index materiality

def _index(response, ticker="NIFTY BANK"):
    return {"ticker": ticker, "segment": "banking index", "direction": "positive", "revenue_effect_cr": None,
            "annual_revenue": None, "price_response_pct": response, "price_response_source": TAG_LLM}


def test_an_index_is_judged_on_its_price_response_not_revenue():
    m = materiality.assess(_index([1.5, 2.5, 3.5]), 2.0)
    assert m["passes_gate"] and m["basis"] == "price_response" and m["effect_pct"] == [1.5, 2.5, 3.5]
    assert m["source"] == TAG_LLM
    assert not materiality.assess(_index([0.5, 1.0, 1.5]), 2.0)["passes_gate"]
    assert "straddles zero" in materiality.assess(_index([-1.0, 2.5, 4.0]), 2.0)["reason"]
    assert materiality.assess(_index(None), 2.0)["reason"] == "index: no price-response range"
    assert materiality.is_index("NIFTY 50") and materiality.is_index("BANKNIFTY") and not materiality.is_index("SBIN")


def test_an_index_justified_move_is_its_price_response_unscaled():
    m = materiality.assess(_index([1.5, 2.5, 3.5]), 2.0)
    bars = [{"session": "2026-10-21", "close": 100.0}, {"session": "2026-10-22", "close": 100.5}]
    p = priced_in.assess("NIFTY BANK", m, "2026-10-22", bars_fn=lambda s: bars, cfg=dict(CFG, move_per_revenue_pct=3.0))
    assert p["justified_move_pct"] == 2.5 and p["verdict"] == "underreaction" and p["direction"] == "bullish"


# --------------------------------------------------------------- re-assessment of recent cards

def _card(tmp_path, event_id, generated, announced, published, priced_reason, suggestions=None):
    exposure = {"ticker": "SCI", "segment": "shipping", "direction": "positive", "revenue_effect_cr": [50, 150, 300],
                "revenue_effect_source": TAG_LLM, "annual_revenue": {"value": 5000.0, "source": TAG_LLM},
                "price_response_pct": [1, 3, 6], "price_response_source": TAG_LLM, "horizon_days": 20}
    card = {"schema": "event_card/1.1", "event_id": event_id, "generated_at": generated,
            "source": {"title": "t", "url": "u", "from": "rss:x", "published": published},
            "announced_on": announced, "exposure": [exposure],
            "materiality": [materiality.assess(exposure, 2.0)],
            "priced_in": [{"ticker": "SCI", "verdict": "unknown", "event_move_pct": None, "justified_move_pct": None,
                           "direction": None, "source": "estimate:computed", "reason": priced_reason}],
            "suggestions": suggestions or [], "flags": []}
    (tmp_path / f"{event_id}.json").write_text(json.dumps(card))
    return card


BARS = [{"session": "2026-10-21", "close": 100.0}, {"session": "2026-10-22", "close": 100.4},
        {"session": "2026-10-23", "close": 100.6}]


def test_reassess_resolves_a_morning_card_once_the_close_is_in_the_lake(tmp_path, monkeypatch):
    monkeypatch.setattr(emit, "STATE_DIR", tmp_path / "state")
    cards = tmp_path / "cards"
    cards.mkdir()
    _card(cards, "other-sci", "2026-10-22T08:41:09+05:30", "2026-10-22", "Thu, 22 Oct 2026 01:17:15 +0530",
          "no prices around the announcement in the bhavcopy lake")
    shipped = []
    out = emit.reassess(card_dir=cards, bars_fn=lambda s: BARS, ship_fn=lambda p: shipped.append(p.name) or True,
                        cfg=dict(CFG), today=date(2026, 10, 23))
    assert out["checked"] == 1 and out["shipped"] == 1 and shipped == ["event_card__other-sci.json"]
    card = json.loads((cards / "other-sci.json").read_text())
    p = card["priced_in"][0]
    assert p["verdict"] == "underreaction" and p["window"] == ["2026-10-21", "2026-10-23"]
    assert card["suggestions"][0]["ticker"] == "SCI" and card["reassessed_at"]
    assert card["generated_at"] == "2026-10-22T08:41:09+05:30"          # the executor's age bound is unchanged
    again = emit.reassess(card_dir=cards, bars_fn=lambda s: BARS, ship_fn=lambda p: True, cfg=dict(CFG),
                          today=date(2026, 10, 23))
    assert again["checked"] == 0                                         # now it suggests: never re-read


def test_reassess_corrects_a_historical_date_and_leaves_old_or_resolved_cards_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(emit, "STATE_DIR", tmp_path / "state")
    _card(tmp_path, "sebi-nam", "2026-10-23T08:40:25+05:30", "2025-02-12", "Fri, 23 Oct 2026 06:41:46 +0530",
          "no prices around the announcement in the bhavcopy lake")
    _card(tmp_path, "old", "2026-10-15T08:40:00+05:30", "2026-10-15", "Thu, 15 Oct 2026 07:00:00 +0530",
          "no prices around the announcement in the bhavcopy lake")
    resolved = _card(tmp_path, "done", "2026-10-22T08:40:00+05:30", "2026-10-22", "Thu, 22 Oct 2026 07:00:00 +0530",
                     None)
    resolved["priced_in"][0].update(verdict="in_line", reason=None)
    (tmp_path / "done.json").write_text(json.dumps(resolved))
    out = emit.reassess(card_dir=tmp_path, bars_fn=lambda s: BARS[:2], do_ship=False, cfg=dict(CFG),
                        today=date(2026, 10, 23))
    assert out["checked"] == 2 and [u["event_id"] for u in out["updated"]] == ["sebi-nam"]
    nam = json.loads((tmp_path / "sebi-nam.json").read_text())
    assert nam["announced_on"] == "2026-10-23" and "historical reference" in nam["flags"][0]
    assert nam["priced_in"][0]["verdict"] == "unknown"                  # 10-23's close is not in the lake yet
    assert json.loads((tmp_path / "old.json").read_text())["announced_on"] == "2026-10-15"
    assert json.loads((tmp_path / "done.json").read_text()) == resolved


# --------------------------------------------------------------- learner: one per session

def _book():
    from src.execution import live_pricer as lp
    c = sqlite3.connect(":memory:")
    lp.ensure_schema(c)
    return c


def _opened(c, ticker, strategy, opened_at, state="open"):
    c.execute("INSERT INTO paper_live_positions (account_id, journal_ref, ticker, strategy, direction, expiry, lots, "
              "lot_size, legs_json, entry_mark_ps, width_ps, max_profit_ps, max_loss_ps, opened_at, state) "
              "VALUES (?, ?, ?, ?, 'neutral', '2026-10-27', 1, 400, '[]', 1, 2, 1, 1, ?, ?)",
              (sl.ACCOUNT, f"{ticker}{strategy}{opened_at}", ticker, strategy, opened_at, state))


def test_the_learner_takes_one_entry_per_underlying_strategy_and_session(monkeypatch):
    monkeypatch.setattr("src.config.SHADOW_LEARNER_MAX_OPEN", None)
    c = _book()
    now = datetime(2026, 10, 22, 10, 30, tzinfo=sl.IST).timestamp()
    assert sl.session_day(now) == "2026-10-22"
    assert sl.ready("INFY.NS", now=now, conn=c, strategy="iron_butterfly") == (True, "ready")
    _opened(c, "INFY.NS", "iron_butterfly", "2026-10-22T10:23:59", state="closed")
    ok, why = sl.ready("INFY.NS", now=now, conn=c, strategy="iron_butterfly")
    assert not ok and why == "learner already took iron_butterfly on INFY.NS this session (2026-10-22)"
    assert sl.ready("INFY.NS", now=now, conn=c, strategy="iron_condor")[0]          # another structure
    assert sl.ready("TCS.NS", now=now, conn=c, strategy="iron_butterfly")[0]        # another underlying
    nxt = (datetime(2026, 10, 23, 9, 20, tzinfo=sl.IST)).timestamp()
    assert sl.ready("INFY.NS", now=nxt, conn=c, strategy="iron_butterfly")[0]       # the next session


# --------------------------------------------------------------- executor switch + fo_bhavcopy read

def test_the_paper_events_executor_is_switched_on():
    from pathlib import Path
    cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text())
    assert cfg["event_engine"]["execute_enabled"] is True and cfg["event_engine"]["max_loss_per_trade_rs"] == 5000


def test_the_fo_feed_is_dated_by_the_shipped_liquidity_file_where_the_raw_lake_is_absent(tmp_path):
    now = datetime(2026, 10, 23, 17, 0)
    assert sg.check("fo_bhavcopy_lake", now=now, root=tmp_path)["state"] == sg.MISSING
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "fo_liquidity.json").write_text(json.dumps({"as_of": "2026-10-22"}))
    v = sg.check("fo_bhavcopy_lake", now=now, root=tmp_path)
    assert v["state"] == sg.FRESH and v["signal"] == "content_date:data/fo_liquidity.json"
    (tmp_path / "data" / "fo_liquidity.json").write_text(json.dumps({"as_of": "2026-10-12"}))
    assert sg.check("fo_bhavcopy_lake", now=now, root=tmp_path)["state"] == sg.STALE     # a fresh file on an old lake
    lake = tmp_path / "data" / "lake" / "fo_bhavcopy"
    lake.mkdir(parents=True)                                                          # the Mac: the raw lake, fresh
    assert sg.check("fo_bhavcopy_lake", now=datetime.now() + timedelta(hours=1), root=tmp_path)["signal"] == "mtime"
