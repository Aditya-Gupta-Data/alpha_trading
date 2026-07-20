"""
The 7-Tier grading engine, fully offline: precedence per approved
mechanical definitions (near-zone 5%, momentum via DMAs, losing volume
20v60, near-stop 1 ATR), Tier-0 NULL-honesty, watch's forensic override,
pin behavior (No-Orphan rule) + stale-pin clearing, and the
family-transition-only card policy.
"""
import json

from src.analysis import darling_tiers as DT


def _level(sym, close=100.0, zone=(95.0, 105.0), stop=88.0, atr=4.0,
           d50=None, d200=None, ext="normal", trail=None):
    return {"symbol": sym, "status": "ok", "close": close,
            "buy_zone": list(zone), "stop": stop, "atr14": atr,
            "dma50": d50, "dma200": d200, "extension": ext,
            "trailing_floor": trail}


def _paths(tmp_path, scores: dict, levels: list, flagged=()):
    """Write queue/levels/valuation fixtures; scores maps sym -> score
    (None = vetoed at valuation)."""
    syms = sorted(scores)
    q = tmp_path / "q.json"
    q.write_text(json.dumps({
        "tickers": syms,
        "passed": [{"symbol": s, "forensic": {"score": 70}}
                   for s in syms if s not in flagged],
        "flagged": [{"symbol": s, "forensic": {"score": 52}}
                    for s in syms if s in flagged]}))
    lv = tmp_path / "levels.json"
    lv.write_text(json.dumps({"levels": levels}))
    val = tmp_path / "val.json"
    val.write_text(json.dumps({"scores": {
        s: {"score": sc} for s, sc in scores.items() if sc is not None}}))
    return {"queue_path": q, "levels_path": lv, "valuation_path": val}


def _tier_of(result, sym):
    for t, rows in result["tiers"].items():
        for r in rows:
            if r["symbol"] == sym:
                return t, r
    return None, None


def test_buy_tiers_and_the_5pct_near_zone_rule(tmp_path):
    paths = _paths(tmp_path,
                   {"DEEP": 20, "FAIR": 40, "NEAR": 30, "FAR": 30},
                   [_level("DEEP"), _level("FAIR"),
                    _level("NEAR", close=108.0),     # 105 < 108 <= 110.25
                    _level("FAR", close=112.0)])     # beyond the 5% band
    b = DT.build(**paths, pins={}, turnover={}, prev_tiers={})
    assert _tier_of(b, "DEEP")[0] == "strong_buy"
    assert _tier_of(b, "FAIR")[0] == "weak_buy"
    t, row = _tier_of(b, "NEAR")
    assert t == "weak_buy" and row["in_zone"] is False \
        and row["near_zone"] is True
    assert _tier_of(b, "FAR")[0] not in ("strong_buy", "weak_buy")


def test_overextension_never_grades_a_buy(tmp_path):
    paths = _paths(tmp_path, {"HOT": 20},
                   [_level("HOT", ext="overextended", d50=90.0, d200=80.0)])
    b = DT.build(**paths, pins={}, turnover={}, prev_tiers={})
    t, _ = _tier_of(b, "HOT")
    assert t not in ("strong_buy", "weak_buy")     # Law 3 stands


def test_sell_tiers(tmp_path):
    paths = _paths(tmp_path,
                   {"RICH": 90, "BROKE": 50, "SLIPPED": 30, "FADING": 75},
                   [_level("RICH", close=120.0),
                    _level("BROKE", close=80.0),     # below the 88 stop
                    _level("SLIPPED", close=92.0),   # below zone, above stop
                    _level("FADING", close=120.0)])
    b = DT.build(**paths, pins={},
                 turnover={"FADING": {"losing_volume": True}},
                 prev_tiers={})
    assert _tier_of(b, "RICH")[0] == "strong_sell"       # valuation >= 85
    assert _tier_of(b, "BROKE")[0] == "strong_sell"      # under the stop
    assert _tier_of(b, "SLIPPED")[0] == "weak_sell"      # zone break
    assert _tier_of(b, "FADING")[0] == "weak_sell"       # volume + val>=70


def test_hold_tiers_momentum_near_stop_and_fallback(tmp_path):
    paths = _paths(tmp_path,
                   {"PRICY": 75, "RUNNER": 50, "DRIFTER": 50, "COILED": 50},
                   [_level("PRICY", close=120.0),
                    _level("RUNNER", close=120.0, d50=110.0, d200=100.0),
                    _level("DRIFTER", close=120.0, d50=125.0, d200=100.0),
                    _level("COILED", close=120.0, d50=110.0, d200=100.0,
                           trail=118.0)])           # within 1 ATR of trail
    b = DT.build(**paths, pins={}, turnover={}, prev_tiers={})
    t, r = _tier_of(b, "PRICY")
    assert t == "weak_hold" and "tighten trails" in r["rule"]
    assert _tier_of(b, "RUNNER")[0] == "strong_hold"
    assert _tier_of(b, "DRIFTER")[0] == "weak_hold"      # honest fallback
    t, r = _tier_of(b, "COILED")                          # near-stop beats
    assert t == "weak_hold" and "ATR of the stop" in r["rule"]


def test_tier0_ungraded_is_honest(tmp_path):
    paths = _paths(tmp_path, {"NOVAL": None, "NOLEVELS": 30},
                   [_level("NOVAL")])                # NOLEVELS has no row
    b = DT.build(**paths, pins={}, turnover={}, prev_tiers={})
    t, r = _tier_of(b, "NOVAL")
    assert t == "ungraded" and "valuation" in r["rule"]
    t, r = _tier_of(b, "NOLEVELS")
    assert t == "ungraded" and "levels" in r["rule"]


def test_watch_overrides_even_a_deep_discount(tmp_path):
    paths = _paths(tmp_path, {"SHADY": 15}, [_level("SHADY")],
                   flagged={"SHADY"})
    b = DT.build(**paths, pins={}, turnover={}, prev_tiers={})
    t, r = _tier_of(b, "SHADY")
    assert t == "watch"          # never mechanically a Buy; human call
    assert r["in_zone"] is True and r["valuation"] == 15  # facts recorded


def test_pins_override_and_keep_dropped_names_visible(tmp_path):
    # GONE is NOT in the queue (weekly dropped it) — the pin keeps it in
    # the table at its pinned grade.
    paths = _paths(tmp_path, {"AAA": 20}, [_level("AAA")])
    pins = {"GONE": {"grade": "strong_sell", "pinned_on": "2026-07-25",
                     "reason": "weekly re-screen rejected: profit streak broke"},
            "FOGGY": {"grade": "ungraded", "pinned_on": "2026-07-25",
                      "reason": "weekly re-screen: data insufficient"}}
    b = DT.build(**paths, pins=pins, turnover={}, prev_tiers={})
    t, r = _tier_of(b, "GONE")
    assert t == "strong_sell" and "pinned" in r["rule"]
    assert r["pinned"].startswith("weekly re-screen rejected")
    assert _tier_of(b, "FOGGY")[0] == "ungraded"    # never a manufactured sell


def test_run_clears_pins_whose_shadow_closed(tmp_path):
    paths = _paths(tmp_path, {"AAA": 20}, [_level("AAA")])
    pins_path = tmp_path / "pins.json"
    pins_path.write_text(json.dumps({"pins": {
        "GONE": {"grade": "strong_sell", "pinned_on": "2026-07-25",
                 "reason": "weekly re-screen rejected: x"}}}))
    # Shadow still open -> pin survives, name stays graded strong_sell.
    r1 = DT.run(**paths, pins_path=pins_path,
                tiers_path=tmp_path / "t.json", prev_tiers={},
                turnover={}, open_fn=lambda: {"GONE"},
                broadcast_fn=lambda c: None)
    assert _tier_of(r1, "GONE")[0] == "strong_sell"
    # Shadow closed -> pin cleared, name drops off the table completely.
    r2 = DT.run(**paths, pins_path=pins_path,
                tiers_path=tmp_path / "t.json", prev_tiers=r1,
                turnover={}, open_fn=lambda: set(),
                broadcast_fn=lambda c: None)
    assert r2["pins_cleared"] == ["GONE"]
    assert _tier_of(r2, "GONE")[0] is None
    assert json.loads(pins_path.read_text())["pins"] == {}


def test_cards_fire_on_family_transitions_only(tmp_path):
    cards = []
    paths = _paths(tmp_path, {"AAA": 20}, [_level("AAA")])
    common = dict(pins_path=tmp_path / "pins.json",
                  tiers_path=tmp_path / "t.json", turnover={},
                  open_fn=lambda: set(), broadcast_fn=cards.append)
    # First grading: ONE distribution summary, no per-name flood.
    r1 = DT.run(**paths, **common, prev_tiers={})
    assert r1["first_grading"] and len(cards) == 1
    assert "first grading" in cards[0]["description"]
    # Same state: silent.
    r2 = DT.run(**paths, **common, prev_tiers=r1)
    assert r2["card_fired"] is False and len(cards) == 1
    # Intrafamily move (strong_buy -> weak_buy): recorded, still silent.
    paths2 = _paths(tmp_path, {"AAA": 40}, [_level("AAA")])
    r3 = DT.run(**paths2, **common, prev_tiers=r2)
    assert [t["family_change"] for t in r3["transitions"]] == [False]
    assert r3["card_fired"] is False and len(cards) == 1
    # Family move (buy -> hold): ONE card naming the move.
    paths3 = _paths(tmp_path, {"AAA": 50}, [_level("AAA")])
    r4 = DT.run(**paths3, **common, prev_tiers=r3)
    assert r4["card_fired"] is True and len(cards) == 2
    assert "buy → hold" in cards[1]["description"]
