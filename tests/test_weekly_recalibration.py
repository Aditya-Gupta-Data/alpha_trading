"""
The Saturday fundamental clock, fully offline: queue diffing, the
No-Orphan pin rules (rejected+held -> strong_sell pin; data-lost+held ->
ungraded pin — never a manufactured sell; unheld -> plain drop;
re-passed -> pin cleared), rebuild orchestration, and the one weekly
summary card.
"""
import json

from src.analysis import weekly_recalibration as WR


def _setup(tmp_path, old_tickers, old_pins=None):
    q = tmp_path / "queue.json"
    q.write_text(json.dumps({"tickers": old_tickers}))
    p = tmp_path / "pins.json"
    if old_pins is not None:
        p.write_text(json.dumps({"pins": old_pins}))
    return q, p


def _screen(tickers, rejected=None, insufficient=()):
    def run(write=True, **kw):
        return {"tickers": tickers, "screened": 99,
                "rejected": rejected or {},
                "insufficient_data": list(insufficient)}
    return run


def _noop_tiers(write=True, broadcast=True):
    return {"counts": {"strong_buy": 1}}


def test_dropped_names_pin_by_failure_flavor(tmp_path):
    q, p = _setup(tmp_path, ["AAA", "BBB", "CCC", "DDD"])
    cards = []
    rep = WR.recalibrate(
        skip_refresh=True,
        screen_fn=_screen(["AAA"], rejected={"BBB": "profit streak broke"},
                          insufficient=["CCC"]),
        pricer_fn=lambda: None, valuation_fn=lambda: None,
        tiers_fn=_noop_tiers,
        open_fn=lambda: {"BBB", "CCC"},      # DDD dropped but NOT held
        broadcast_fn=cards.append, queue_path=q, pins_path=p)
    pins = rep["pins"]
    assert pins["BBB"]["grade"] == "strong_sell"
    assert "profit streak broke" in pins["BBB"]["reason"]
    assert pins["CCC"]["grade"] == "ungraded"    # data loss ≠ a sell verdict
    assert "DDD" not in pins                     # nothing open to protect
    assert rep["dropped"] == ["BBB", "CCC", "DDD"]
    # Pins persisted for the daily clock to honor.
    assert set(json.loads(p.read_text())["pins"]) == {"BBB", "CCC"}
    # The one weekly card names the pins.
    assert len(cards) == 1
    assert "pinned strong_sell: BBB" in cards[0]["description"]


def test_repassed_and_closed_out_pins_clear(tmp_path):
    old_pins = {"BBB": {"grade": "strong_sell", "pinned_on": "2026-07-18",
                        "reason": "weekly re-screen rejected: x"},
                "CCC": {"grade": "strong_sell", "pinned_on": "2026-07-18",
                        "reason": "weekly re-screen rejected: y"}}
    q, p = _setup(tmp_path, ["AAA"], old_pins)
    rep = WR.recalibrate(
        skip_refresh=True,
        screen_fn=_screen(["AAA", "BBB"]),   # BBB re-passed the screen
        pricer_fn=lambda: None, valuation_fn=lambda: None,
        tiers_fn=_noop_tiers,
        open_fn=lambda: {"BBB"},             # CCC's shadow closed
        broadcast_fn=lambda c: None, queue_path=q, pins_path=p)
    assert rep["pins"] == {}                 # both cleared, different doors
    assert rep["pins_cleared"] == ["BBB", "CCC"]


def test_carried_pin_keeps_its_original_story(tmp_path):
    original = {"BBB": {"grade": "strong_sell", "pinned_on": "2026-07-11",
                        "reason": "weekly re-screen rejected: EPS shrank"}}
    q, p = _setup(tmp_path, ["AAA"], original)
    rep = WR.recalibrate(
        skip_refresh=True, screen_fn=_screen(["AAA"]),
        pricer_fn=lambda: None, valuation_fn=lambda: None,
        tiers_fn=_noop_tiers, open_fn=lambda: {"BBB"},
        broadcast_fn=lambda c: None, queue_path=q, pins_path=p)
    assert rep["pins"]["BBB"] == original["BBB"]   # date + reason intact


def test_screen_failure_is_fail_open_and_pins_nothing(tmp_path):
    q, p = _setup(tmp_path, ["AAA", "BBB"])

    def broken(write=True, **kw):
        raise RuntimeError("NSE down")

    rep = WR.recalibrate(
        skip_refresh=True, screen_fn=broken,
        pricer_fn=lambda: None, valuation_fn=lambda: None,
        tiers_fn=_noop_tiers, open_fn=lambda: {"AAA", "BBB"},
        broadcast_fn=lambda c: None, queue_path=q, pins_path=p)
    assert rep["queue_unchanged"] is True
    assert rep["dropped"] == [] and rep["pins"] == {}
    assert any("screen" in e for e in rep["errors"])
