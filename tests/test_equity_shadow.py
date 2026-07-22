"""Shadow Equity Engine (PAPER_TELEMETRY) — hermetic contract tests.

Everything is injected (deals / quotes / vix / universe / ledger path);
no network, no real ledgers, no journal. Under test: the owner's
four-question learning frame (kyu_trigger / kaise_context /
kya_kara_action / kya_sikha_autopsy), telemetry-only flags on every event,
the block-VWAP pullback entry logic, one-open-per-ticker, exit autopsy
categorization, and the market_loop hook staying fail-open and OFF by
default.
"""
import asyncio
import json

import pytest

from src import equity_shadow_proposer as shadow
from src import knowledge_graph_logger as kg


def _deals():
    """A ledger slice that yields a block-VWAP floor at exactly 100: two
    block buys (1000 @ 99 / 1000 @ 101), no sells -> accumulation True."""
    return [
        {"ticker": "TCS.NS", "client": "BIG FUND A", "side": "buy",
         "qty": 1000, "price": 99.0, "value_rs": 99000.0,
         "deal_type": "block", "as_of": "2026-07-10"},
        {"ticker": "TCS.NS", "client": "BIG FUND B", "side": "buy",
         "qty": 1000, "price": 101.0, "value_rs": 101000.0,
         "deal_type": "block", "as_of": "2026-07-14"},
    ]


UNIVERSE = {"IT": {"yahoo_index": "^CNXIT",
                   "constituents": ["TCS.NS", "INFY.NS"]}}


STUB_SECTOR = {"sector": "IT", "bullish": None}
STUB_NIFTY = {"uptrend": True, "rsi": 55.0, "fresh_cross": False}


def _cycle(tmp_path, price, deals=None, vix=13.5):
    path = tmp_path / "ledger.jsonl"
    res = shadow.run_cycle(
        deals_by_ticker={"TCS.NS": deals if deals is not None else _deals()},
        quote_fn=lambda t: price, vix_fn=lambda: vix,
        universe=UNIVERSE, path=path,
        sector_fn=lambda t: dict(STUB_SECTOR),
        nifty_trend_fn=lambda: dict(STUB_NIFTY))
    return res, path


def test_entry_carries_the_four_question_frame(tmp_path):
    res, path = _cycle(tmp_path, price=102.0)
    assert len(res["entries"]) == 1 and res["exits"] == []
    e = res["entries"][0]
    # telemetry contract
    assert e["mode"] == "PAPER_TELEMETRY"
    assert e["capital_allocated"] == 0
    # KYU (why): the exact alpha signal
    kyu = e["kyu_trigger"]
    assert kyu["setup"] == "block_vwap_pullback"
    assert kyu["block_vwap"] == 100.0   # (1000*99 + 1000*101) / 2000
    assert kyu["accumulation"] is True
    assert len(kyu["trigger_deals"]) == 2
    assert "BIG FUND B" in kyu["signal"]      # anchor block named
    assert "floor" in kyu["signal"]
    # KAISE (how): market context at entry
    kaise = e["kaise_context"]
    assert kaise["vix"] == 13.5
    assert kaise["sector"]["sector"] == "IT"
    assert kaise["nifty_trend"]["uptrend"] is True
    # KYA KARA (what we did)
    act = e["kya_kara_action"]
    assert act["side"] == "long"
    assert act["entry_price"] == 102.0
    assert act["stop"] == 98.0                # floor * 0.98
    assert act["target"] == 110.0             # 102 + 2*(102-98)
    assert act["simulated_risk_pct"] == round(4 / 102 * 100, 2)
    # persisted as one JSONL line
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["event"] == "entry"


@pytest.mark.parametrize("price,why", [
    (99.0, "below the floor is a breakdown, not a pullback"),
    (106.0, "more than 5% above the floor is not a pullback"),
    (None, "no live quote means no decision"),
])
def test_no_entry_outside_the_pullback_band(tmp_path, price, why):
    res, path = _cycle(tmp_path, price=price)
    assert res["entries"] == [], why
    assert not path.exists()


def test_distribution_context_is_recorded_not_gating(tmp_path):
    """Telemetry calibration: net sellers do NOT block the entry (required
    confirmation measured ~0 qualifying names on the real ledger) — the
    accumulation flag is recorded so outcome analysis can judge it."""
    deals = _deals() + [{
        "ticker": "TCS.NS", "client": "BIG FUND C", "side": "sell",
        "qty": 5000, "price": 100.0, "value_rs": 500000.0,
        "deal_type": "block", "as_of": "2026-07-15"}]
    res, _ = _cycle(tmp_path, price=102.0, deals=deals)
    assert len(res["entries"]) == 1
    assert res["entries"][0]["kyu_trigger"]["accumulation"] is False
    assert res["entries"][0]["kyu_trigger"]["net_value_rs"] < 0


def test_no_entry_without_a_block_floor(tmp_path):
    bulk_only = [dict(d, deal_type="bulk") for d in _deals()]
    res, path = _cycle(tmp_path, price=102.0, deals=bulk_only)
    assert res["entries"] == []   # no block floor = no thesis to falsify
    assert not path.exists()


def test_one_open_shadow_per_ticker(tmp_path):
    res1, path = _cycle(tmp_path, price=102.0)
    assert len(res1["entries"]) == 1
    res2 = shadow.run_cycle(deals_by_ticker={"TCS.NS": _deals()},
                            quote_fn=lambda t: 102.0, vix_fn=lambda: 13.5,
                            universe=UNIVERSE, path=path,
                            sector_fn=lambda t: dict(STUB_SECTOR),
                            nifty_trend_fn=lambda: None)
    assert res2["entries"] == []  # already open — no duplicate


@pytest.mark.parametrize("exit_price,reason,category_frag,r_sign", [
    (97.9, "stop_loss", "VWAP defense failed", -1),
    (95.0, "stop_loss", "Gap-down shock", -1),    # >2% through the stop
    (111.0, "target", "Target hit", +1),
])
def test_exit_autopsy_categorizes_the_outcome(tmp_path, exit_price, reason,
                                              category_frag, r_sign):
    res1, path = _cycle(tmp_path, price=102.0)
    entry = res1["entries"][0]
    res2 = shadow.run_cycle(deals_by_ticker={"TCS.NS": _deals()},
                            quote_fn=lambda t: exit_price,
                            vix_fn=lambda: 19.0,
                            universe=UNIVERSE, path=path,
                            sector_fn=lambda t: dict(STUB_SECTOR),
                            nifty_trend_fn=lambda: None)
    assert len(res2["exits"]) == 1
    x = res2["exits"][0]
    assert x["mode"] == "PAPER_TELEMETRY" and x["capital_allocated"] == 0
    assert x["reason"] == reason
    assert x["id"] == entry["id"]                 # paired to its entry
    autopsy = x["kya_sikha_autopsy"]
    assert category_frag in autopsy["category"]
    assert (autopsy["r_multiple"] > 0) == (r_sign > 0)
    assert autopsy["below_block_vwap"] == (exit_price < 100.0)
    assert autopsy["vix_at_exit"] == 19.0
    assert autopsy["sector_at_exit"]["sector"] == "IT"
    # ledger shows no open positions; same-day re-entry blocked
    assert kg.open_positions(path=path) == {}
    assert res2["entries"] == []


def test_sector_drag_category_when_sector_is_bearish(tmp_path):
    entry = {
        "event": "entry", "id": "abcd1234", "mode": "PAPER_TELEMETRY",
        "capital_allocated": 0, "ticker": "TCS.NS", "as_of": "2026-07-16",
        "kyu_trigger": {"block_vwap": 100.0},
        "kya_kara_action": {"entry_price": 102.0, "stop": 98.0,
                            "target": 110.0},
    }
    cat = shadow.categorize_failure("stop_loss", 97.9, entry,
                                    sector_bullish_at_exit=False)
    assert "sector dragged" in cat
    # gap through the stop outranks the sector read
    cat = shadow.categorize_failure("stop_loss", 90.0, entry,
                                    sector_bullish_at_exit=False)
    assert "Gap-down shock" in cat


def test_time_stop_closes_stale_theses(tmp_path):
    path = tmp_path / "ledger.jsonl"
    shadow.run_cycle(deals_by_ticker={"TCS.NS": _deals()},
                     quote_fn=lambda t: 102.0, vix_fn=lambda: 13.5,
                     universe=UNIVERSE, path=path)
    events = kg.read_events(path)
    events[0]["as_of"] = "2026-07-01"   # age the entry 16 calendar days
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    exits = shadow.track_open_shadows(quote_fn=lambda t: 103.0,
                                      vix_fn=lambda: 13.5,
                                      universe=UNIVERSE, path=path,
                                      sector_fn=lambda t: dict(STUB_SECTOR))
    assert len(exits) == 1 and exits[0]["reason"] == "time_stop"
    assert "Time stop" in exits[0]["kya_sikha_autopsy"]["category"]


def test_shadow_never_imports_the_real_book():
    import src.equity_shadow_proposer as mod
    src = open(mod.__file__).read()
    for forbidden in ("journal", "portfolio_manager", "options_proposer",
                      "notifier", "brain_map"):
        assert f"from src import {forbidden}" not in src
        assert f"from src.{forbidden}" not in src
        assert f"import src.{forbidden}" not in src


def test_market_loop_hook_is_off_by_default_and_fail_open(tmp_path, monkeypatch):
    from src.market_loop import run_market_loop
    # Isolate the cooldown seed from the REAL journal (2026-07-22 fix): the
    # edge miner refreshes data/journal.jsonl from the VM, so a real NIFTY 50
    # entry can drift inside this test's pinned 11:00 window and cooldown-block
    # fetch_fn — the assertion then fails on live data, not on code.
    from src import journal
    monkeypatch.setattr(journal, "JOURNAL_PATH", tmp_path / "journal.jsonl")

    calls = {"shadow": 0, "fetch": 0}

    def boom():
        calls["shadow"] += 1
        raise RuntimeError("shadow exploded")

    def fake_fetch(u):
        calls["fetch"] += 1
        return None

    async def one_cycle(shadow_fn):
        task = asyncio.create_task(run_market_loop(
            underlyings=("NIFTY 50",), interval=0.01,
            fetch_fn=fake_fetch, propose_fn=lambda u, s: {"proposed": False},
            now_fn=lambda: __import__("datetime").datetime(
                2026, 7, 17, 11, 0,
                tzinfo=__import__("src.market_loop", fromlist=["IST"]).IST),
            shadow_fn=shadow_fn))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # OFF by default: shadow_fn=None -> never called
    asyncio.run(one_cycle(None))
    assert calls["shadow"] == 0 and calls["fetch"] > 0
    # ON and exploding: loop keeps cycling anyway
    calls["fetch"] = 0
    asyncio.run(one_cycle(boom))
    assert calls["shadow"] > 0 and calls["fetch"] > 0
