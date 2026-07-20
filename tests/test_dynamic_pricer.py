"""
Phase 2 of the Darling Pipeline, fully offline:

  * dynamic_pricer — ATR/DMA NULL-honesty, the volume anchor, anchored
    VWAP, high-volume nodes, confirmed-only pivots, the Law-3
    overextension state (with honest abstain), levels_for and the run()
    that writes darlings_levels.json + the shadow journal.
  * equity_entry_checks — the composed halt stack: never-short-a-darling
    (non-negotiable), fail-closed liquidity, the expiry-week physical-
    settlement defense, and the overextension halt. The 1R gate is
    PARKED by owner order and deliberately absent.
"""
import json
from datetime import date

from src.analysis import dynamic_pricer as DP
from src.analysis import equity_entry_checks as EC


def _bars(n=220, start=100.0, drift=0.3, vol=1_000_000, spike_at=None):
    bars, price = [], start
    for i in range(n):
        price += drift
        v = vol * (5 if i == spike_at else 1)
        bars.append({"session": f"2026-{(i//30)+1:02d}-{(i%28)+1:02d}",
                     "open": price - 0.5, "high": price + 1.0,
                     "low": price - 1.0, "close": price,
                     "prev_close": price - drift, "volume": v})
    return bars


# ------------------------------------------------------------ pure math

def test_atr_and_dma_are_null_honest():
    bars = _bars(220)
    assert DP.atr(bars) is not None and DP.atr(bars) > 0
    assert DP.atr(bars[:10]) is None            # < n+1 bars
    assert DP.dma(bars, 200) is not None
    assert DP.dma(bars[:150], 200) is None      # honest abstain


def test_anchor_finds_the_volume_spike_up_day():
    bars = _bars(100, spike_at=80)
    assert DP.find_anchor(bars) == 80
    vwap = DP.anchored_vwap(bars, 80)
    assert vwap is not None
    closes = [b["close"] for b in bars[80:]]
    assert min(closes) <= vwap <= max(closes)


def test_high_volume_nodes_and_thin_history():
    nodes = DP.high_volume_nodes(_bars(120, spike_at=100))
    assert nodes and all(lo < hi for lo, hi in nodes)
    assert DP.high_volume_nodes(_bars(10)) == []    # honest empty


def test_pivots_confirmed_only():
    bars = _bars(40, drift=0.0)
    bars[20]["high"] = 150.0                    # clear swing high
    bars[25]["low"] = 50.0                      # clear swing low
    pv = DP.pivots(bars)
    assert 150.0 in [h for _, h in pv["highs"]]
    assert 50.0 in [l for _, l in pv["lows"]]
    # the last `flank` bars can never confirm a pivot
    bars[-1]["high"] = 999.0
    assert 999.0 not in [h for _, h in DP.pivots(bars)["highs"]]


def test_extension_state_and_abstain():
    assert DP.extension_state(200.0, 150.0, 120.0, 5.0) == "overextended"
    assert DP.extension_state(151.0, 150.0, 148.0, 5.0) == "normal"
    assert DP.extension_state(200.0, 150.0, None, 5.0) is None  # abstain


def test_levels_for_full_and_thin():
    row = DP.levels_for("GOOD", _bars(220, spike_at=200))
    assert row["status"] == "ok"
    assert row["buy_zone"][0] <= row["buy_zone"][1]
    assert row["stop"] < row["buy_zone"][0]     # stop below the zone
    assert row["dma200"] is not None
    thin = DP.levels_for("THIN", _bars(30))
    assert thin["status"] == "insufficient_history"


def test_run_writes_levels_and_shadow_journal(tmp_path):
    q = tmp_path / "q.json"
    q.write_text(json.dumps({"tickers": ["GOOD", "THIN"]}))
    store = {"GOOD": _bars(220), "THIN": _bars(20)}
    out = DP.run(queue_path=q, bars_fn=lambda s: store[s],
                 levels_path=tmp_path / "levels.json",
                 journal_path=tmp_path / "journal.jsonl")
    assert len(out["levels"]) == 1
    assert out["insufficient_history"] == ["THIN"]
    written = json.loads((tmp_path / "levels.json").read_text())
    assert "ADVISORY-ONLY" in written["advisory_note"]
    lines = (tmp_path / "journal.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1                      # one line per OK symbol
    assert json.loads(lines[0])["symbol"] == "GOOD"


# ------------------------------------------------------- the halt stack

def _seed(tmp_path, tickers=("DARLING",), ext=None):
    q = tmp_path / "q.json"
    q.write_text(json.dumps({"tickers": list(tickers)}))
    lv = tmp_path / "levels.json"
    lv.write_text(json.dumps({"levels": [
        {"symbol": t, "extension": ext} for t in tickers]}))
    return q, lv


def test_never_short_a_darling_is_absolute(tmp_path):
    q, lv = _seed(tmp_path)
    r = EC.check_entry({"symbol": "DARLING", "direction": "short",
                        "instrument": "option"}, queue_path=q,
                       levels_path=lv, today=date(2026, 7, 6))
    assert r["allowed"] is False
    assert r["blocked_by"] == "never_short_darling"
    # shorting a non-darling is not this stack's business
    r2 = EC.check_entry({"symbol": "RANDOM", "direction": "short",
                         "instrument": "delivery"}, queue_path=q,
                        levels_path=lv, today=date(2026, 7, 6))
    assert r2["allowed"] is True


def test_liquidity_filter_fails_closed_for_options(tmp_path):
    q, lv = _seed(tmp_path)
    r = EC.check_entry({"symbol": "DARLING", "direction": "long",
                        "instrument": "option"}, queue_path=q,
                       levels_path=lv, today=date(2026, 7, 6))
    assert r["allowed"] is False
    assert r["blocked_by"] == "liquidity_filter"


def test_expiry_week_halt_blocks_the_final_week(tmp_path):
    # July 2026: last Thursday = 30th. The liquidity filter fires first
    # for options, so test the check directly.
    assert EC.monthly_expiry(date(2026, 7, 5)) == date(2026, 7, 30)
    blocked = EC.expiry_week_halt({"instrument": "option"},
                                  today=date(2026, 7, 27))
    assert blocked[0] is False and "physical" in blocked[1]
    fine = EC.expiry_week_halt({"instrument": "option"},
                               today=date(2026, 7, 10))
    assert fine[0] is True
    delivery = EC.expiry_week_halt({"instrument": "delivery"},
                                   today=date(2026, 7, 27))
    assert delivery[0] is True                  # cash has no expiry


def test_overextension_halts_delivery_buys_but_abstain_does_not(tmp_path):
    q, lv = _seed(tmp_path, ext="overextended")
    r = EC.check_entry({"symbol": "DARLING", "direction": "long",
                        "instrument": "delivery"}, queue_path=q,
                       levels_path=lv, today=date(2026, 7, 6))
    assert r["allowed"] is False
    assert r["blocked_by"] == "overextension_halt"
    q2, lv2 = _seed(tmp_path, ext=None)         # thin history: abstain
    r2 = EC.check_entry({"symbol": "DARLING", "direction": "long",
                         "instrument": "delivery"}, queue_path=q2,
                        levels_path=lv2, today=date(2026, 7, 6))
    assert r2["allowed"] is True


def test_one_r_gate_is_parked_and_absent():
    """Owner order 2026-07-19: the 1R gate is parked — nothing R-related
    exists in this stack."""
    names = [c.__name__ for c in EC.EQUITY_ENTRY_CHECKS]
    assert not any("1r" in n.lower() or "one_r" in n for n in names)


def test_liquidity_filter_wired_to_tiers(tmp_path):
    """2026-07-20: the fail-closed stub became a real tier check."""
    import json as _json
    from datetime import date as _d
    snap = tmp_path / "liq.json"
    snap.write_text(_json.dumps({
        "as_of": "2026-07-17",
        "symbols": {"BIGLIQ": {"tier": "tier1", "rank": 3},
                    "THINQ": {"tier": "tier2", "rank": 30},
                    "CROWDED": {"tier": "banned", "rank": 5}}}))
    today = _d(2026, 7, 20)

    def prop(sym):
        return {"symbol": sym, "direction": "long", "instrument": "option"}

    ok, _ = EC.liquidity_filter(prop("BIGLIQ"), liquidity_path=snap,
                                today=today)
    assert ok is True
    for sym, why in (("THINQ", "tier"), ("CROWDED", "BAN"),
                     ("NOWHERE", "not an F&O")):
        ok, reason = EC.liquidity_filter(prop(sym), liquidity_path=snap,
                                         today=today)
        assert ok is False and why.lower() in reason.lower()
    # stale snapshot -> fail-closed
    old = tmp_path / "old.json"
    old.write_text(_json.dumps({"as_of": "2026-06-01",
                                "symbols": {"BIGLIQ": {"tier": "tier1"}}}))
    ok, reason = EC.liquidity_filter(prop("BIGLIQ"), liquidity_path=old,
                                     today=today)
    assert ok is False and "stale" in reason
    # missing file -> fail-closed (unchanged behaviour)
    ok, _ = EC.liquidity_filter(prop("BIGLIQ"),
                                liquidity_path=tmp_path / "nope.json",
                                today=today)
    assert ok is False
