"""
The Patience Basket join, fully offline: bucketing (ripe needs in-zone
AND cheap AND not overextended), the no-FOMO buckets, newly-ripe
de-duplication against the previous basket, the one-card broadcast that
never re-fires for repeat-ripe names, and honest no_valuation handling.
"""
import json


from src.analysis import patience_basket as PB


def _fixtures(tmp_path, val_scores, extension="normal", close=100.0):
    q = tmp_path / "q.json"
    syms = list(val_scores)
    q.write_text(json.dumps({
        "tickers": syms,
        "passed": [{"symbol": s, "forensic": {"score": 70}} for s in syms],
        "flagged": []}))
    lv = tmp_path / "levels.json"
    lv.write_text(json.dumps({"levels": [
        {"symbol": s, "status": "ok", "close": close,
         "buy_zone": [95.0, 105.0], "stop": 88.0, "extension": extension}
        for s in syms]}))
    val = tmp_path / "val.json"
    val.write_text(json.dumps({"scores": {
        s: {"score": sc} for s, sc in val_scores.items()
        if sc is not None}}))
    return {"queue_path": q, "levels_path": lv, "valuation_path": val}


def test_ripe_needs_in_zone_and_cheap_and_not_overextended(tmp_path):
    paths = _fixtures(tmp_path, {"CHEAPIN": 30, "RICHIN": 80})
    b = PB.build(**paths, prev_basket={})
    assert [r["symbol"] for r in b["ripe"]] == ["CHEAPIN"]
    assert [r["symbol"] for r in b["in_zone_not_cheap"]] == ["RICHIN"]

    hot = _fixtures(tmp_path, {"CHEAPIN": 30}, extension="overextended")
    b2 = PB.build(**hot, prev_basket={})
    assert b2["ripe"] == []                    # Law 3 blocks ripeness


def test_no_fomo_buckets(tmp_path):
    away = _fixtures(tmp_path, {"CHEAPFAR": 20, "WAITER": 70}, close=200.0)
    b = PB.build(**away, prev_basket={})
    assert [r["symbol"] for r in b["cheap_not_in_zone"]] == ["CHEAPFAR"]
    assert [r["symbol"] for r in b["waiting"]] == ["WAITER"]
    below = _fixtures(tmp_path, {"FELL": 30}, close=80.0)
    b2 = PB.build(**below, prev_basket={})
    assert [r["symbol"] for r in b2["below_zone"]] == ["FELL"]


def test_no_valuation_is_honest(tmp_path):
    paths = _fixtures(tmp_path, {"NOVAL": None})
    b = PB.build(**paths, prev_basket={})
    assert [r["symbol"] for r in b["no_valuation"]] == ["NOVAL"]
    assert b["ripe"] == []


def test_newly_ripe_dedupes_and_card_fires_once(tmp_path):
    paths = _fixtures(tmp_path, {"CHEAPIN": 30})
    cards = []
    b1 = PB.run(**paths, basket_path=tmp_path / "b.json",
                prev_basket={}, broadcast_fn=cards.append)
    assert b1["newly_ripe"] == ["CHEAPIN"] and len(cards) == 1
    assert "CHEAPIN" in cards[0]["description"]
    assert "Advisory only" in cards[0]["description"]

    # same state next run: still ripe, NOT newly ripe, no second card
    b2 = PB.run(**paths, basket_path=tmp_path / "b.json",
                prev_basket=b1, broadcast_fn=cards.append)
    assert b2["newly_ripe"] == [] and len(cards) == 1
    assert b2["card_fired"] is False
