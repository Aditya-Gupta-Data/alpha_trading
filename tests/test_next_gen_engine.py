"""next_gen_engine/ (Stage 4/5 staging) — hermetic unit tests.

Pure logic only: no network, no files, no clocks beyond injected dates.
These modules are NOT wired into the live engine; the tests pin their
contracts down so the later canonical merge is mechanical.
"""
import struct

import pytest

from next_gen_engine import dhan_websocket as ws
from next_gen_engine import execution_algo as ex
from next_gen_engine import redis_pubsub as rp
from next_gen_engine import trailing_stops as ts
from next_gen_engine import wisdom_extractor as wis


# ------------------------------------------------------------- trailing stops

def _bars(closes, spread=2.0):
    return [{"high": c + spread / 2, "low": c - spread / 2, "close": c}
            for c in closes]


def test_atr_abstains_on_short_history_and_computes_on_enough():
    assert ts.atr(_bars([100, 101]), period=14) is None
    bars = _bars([100 + i * 0.5 for i in range(20)])
    a = ts.atr(bars, period=14)
    assert a is not None and a > 0


def test_trailing_stop_ratchets_and_never_widens():
    rising = _bars([100 + i for i in range(20)])
    first = ts.update_trailing_stop(None, rising, side="long")
    assert first["stop"] is not None
    # market falls back: raw level drops, ratchet must hold the old stop
    fallen = rising + _bars([110, 105, 102])
    held = ts.update_trailing_stop(first["stop"], fallen, side="long")
    assert held["stop"] >= first["stop"]
    # data gap: uncomputable level retains the previous stop
    kept = ts.update_trailing_stop(77.7, _bars([100, 101]), side="long")
    assert kept["stop"] == 77.7 and "retained" in kept["note"]


def test_short_side_trails_downward_and_hit_check_abstains():
    falling = _bars([100 - i for i in range(20)])
    s = ts.update_trailing_stop(None, falling, side="short")
    assert s["stop"] > falling[-1]["close"]           # stop sits above price
    assert ts.stop_hit(s["stop"], falling[-1]["close"], "short") is False
    assert ts.stop_hit(None, 100.0, "long") is None
    assert ts.stop_hit(95.0, 94.0, "long") is True


# -------------------------------------------------------------- limit chasing

def test_chase_plan_walks_mid_to_touch_on_tick():
    p = ex.plan_limit_chase("buy", top_bid=100.0, top_ask=101.0,
                            window_s=30, steps=6)
    prices = [r["limit_price"] for r in p["rungs"]]
    assert prices[0] == 100.5                          # mid
    assert prices[-1] == 101.0                         # touch
    assert prices == sorted(prices)                    # monotonic walk
    assert all(round(x / 0.05, 6) == round(x / 0.05) for x in prices)
    assert p["rungs"][-1]["t_offset_s"] == 30.0
    sell = ex.plan_limit_chase("sell", 100.0, 101.0)
    sp = [r["limit_price"] for r in sell["rungs"]]
    assert sp[0] == 100.5 and sp[-1] == 100.0          # walks down to bid


def test_chase_refuses_garbage_books():
    assert "error" in ex.plan_limit_chase("buy", 0, 101.0)
    assert "error" in ex.plan_limit_chase("buy", 102.0, 101.0)  # crossed
    assert "error" in ex.plan_limit_chase("hold", 100.0, 101.0)


def test_chase_fill_takes_price_improvement_when_market_comes_in():
    p = ex.plan_limit_chase("buy", 100.0, 101.0, steps=4)
    # ask drops to 100.55 by rung 1: our 100.65 rung crosses -> fill 100.55
    quotes = [{"top_bid": 100.0, "top_ask": 101.0},
              {"top_bid": 100.0, "top_ask": 100.55}]
    f = ex.simulate_chase_fill(p, quotes)
    assert f["filled"] is True and f["fill_price"] == 100.55
    assert f["improvement_vs_touch"] == 0.45
    assert f["fill_basis"] == "chase"


def test_chase_reports_honest_miss_never_a_phantom_fill():
    p = ex.plan_limit_chase("buy", 100.0, 101.0, steps=3)
    runaway = [{"top_bid": 101.5, "top_ask": 102.5}] * 3   # market ran away
    f = ex.simulate_chase_fill(p, runaway)
    assert f["filled"] is False
    assert f["cross_now_price"] == 102.5               # the true cost now
    assert "never auto-filled at mid" in f["note"]


def test_protective_legs_execute_first_and_stably():
    legs = [{"side": "sell", "strike": 25000}, {"side": "buy", "strike": 25200},
            {"side": "sell", "strike": 24800}, {"side": "buy", "strike": 24600}]
    ordered = ex.sequence_spread_legs(legs)
    assert [l["side"] for l in ordered] == ["buy", "buy", "sell", "sell"]
    assert [l["strike"] for l in ordered] == [25200, 24600, 25000, 24800]


# ---------------------------------------------------------- wisdom extractor

class _FakeExtractor:
    """LocalExtractor-shaped stub — no network. Returns a canned dict from
    chat_json, and reachability/raw are configurable per test."""
    def __init__(self, raw, reachable=True):
        self._raw = raw
        self._reachable = reachable
        self.base_url = "fake://"
        self.model = "fake-model"

    def is_reachable(self):
        return self._reachable

    def chat_json(self, system, user):
        return self._raw


def test_wisdom_coerces_a_clean_frame():
    raw = {"target_sector": "energy", "direction": "bullish",
           "timeframe_days": 60, "volatility_regime": "mid",
           "fundamental_filters": ["debt_to_equity<1", "  "],
           "thesis": "Crude tailwind for upstream.", "confidence": 0.8}
    out = wis.extract_wisdom("memo text", extractor=_FakeExtractor(raw),
                             source="memo")
    assert out["ok"] is True and out["source"] == "memo"
    f = out["frame"]
    assert f["target_sector"] == "ENERGY"        # normalised to the vocab
    assert f["direction"] == "bullish"
    assert f["timeframe_days"] == 60
    assert f["volatility_regime"] == "mid"
    assert f["fundamental_filters"] == ["debt_to_equity<1"]  # blank dropped
    assert f["confidence"] == 0.8
    assert f["actionable"] is True
    assert f["_valid"]["target_sector"] and f["_valid"]["direction"]


def test_wisdom_drops_unknown_enums_and_out_of_range_horizon():
    raw = {"target_sector": "CRYPTO", "direction": "moon",
           "timeframe_days": 9999, "volatility_regime": "apocalyptic",
           "fundamental_filters": "not-a-list", "confidence": 5.0}
    f = wis.extract_wisdom("x", extractor=_FakeExtractor(raw))["frame"]
    assert f["target_sector"] is None            # unknown sector dropped
    assert f["direction"] is None                # bad enum dropped
    assert f["timeframe_days"] is None           # out of 1..365
    assert f["volatility_regime"] is None
    assert f["fundamental_filters"] == []        # non-list -> empty
    assert f["confidence"] == 1.0                # clamped into 0..1
    assert f["actionable"] is False


@pytest.mark.parametrize("extractor,reason_frag", [
    (_FakeExtractor(None, reachable=False), "unreachable"),
    (_FakeExtractor("not a dict"), "no parseable JSON"),
])
def test_wisdom_fails_open(extractor, reason_frag):
    out = wis.extract_wisdom("some text", extractor=extractor)
    assert out["ok"] is False and reason_frag in out["reason"]


def test_wisdom_rejects_empty_text_without_calling_the_model():
    out = wis.extract_wisdom("   ", extractor=_FakeExtractor({"x": 1}))
    assert out["ok"] is False and out["reason"] == "empty text"


# ------------------------------------------------------ redis pub/sub adapter

def test_publish_envelopes_onto_the_namespaced_channel():
    broker = rp.FakeBroker()
    pub = rp.EventPublisher(client=broker)
    res = pub.publish("proposal", "created", {"id": "abc", "underlying": "NIFTY"})
    assert res["published"] is True
    assert res["channel"] == "alpha.proposal.created"
    channel, data = broker.published[0]
    assert channel == "alpha.proposal.created"
    import json
    env = json.loads(data)
    assert env["event"] == "proposal.created" and env["v"] == 1
    assert env["payload"]["id"] == "abc" and "ts" in env


def test_subscriber_dispatches_matching_pattern_and_skips_junk():
    got = []
    broker = rp.FakeBroker()
    sub = rp.EventSubscriber(handler=got.append, client=broker)
    sub.subscribe("alpha.proposal.*")
    pub = rp.EventPublisher(client=broker)
    out = pub.publish("proposal", "created", {"id": "z1"})
    # broker reports 1 subscriber matched the pattern
    assert out["subscribers"] == 1
    for msg in broker.deliver("alpha.proposal.created", out["envelope"]
                              and __import__("json").dumps(out["envelope"])):
        assert sub.dispatch(msg) is True
    assert len(got) == 1 and got[0]["event"] == "proposal.created"
    # junk data and non-message frames are skipped, never fatal
    assert sub.dispatch({"type": "pmessage", "data": "{bad json"}) is False
    assert sub.dispatch({"type": "subscribe", "data": None}) is False


def test_handler_exception_does_not_break_the_subscriber():
    def boom(_env):
        raise RuntimeError("consumer bug")
    sub = rp.EventSubscriber(handler=boom, client=rp.FakeBroker())
    msg = {"type": "message", "data": {"event": "x.y", "payload": {}}}
    assert sub.dispatch(msg) is False            # swallowed, returns False


def test_live_transport_requires_the_optional_redis_package(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_redis(name, *a, **k):
        if name == "redis":
            raise ImportError("no redis")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_redis)
    with pytest.raises(RuntimeError, match="redis package not installed"):
        rp.EventPublisher(url="redis://localhost:6379/0").connect()


# ------------------------------------------------------ dhan websocket template

def _ticker_packet(security_id, ltp, segment=1, ltt=1_700_000_000):
    header = struct.pack(ws.HEADER_FMT, ws.CODE_TICKER, 16, segment, security_id)
    return header + struct.pack("<fi", ltp, ltt)


def test_parse_header_and_ticker_roundtrip():
    pkt = _ticker_packet(11536, 102.5)
    h = ws.parse_header(pkt)
    assert h["code"] == ws.CODE_TICKER and h["security_id"] == 11536
    tick = ws.parse_message(pkt)
    assert isinstance(tick, ws.Tick)
    assert tick.last_price == 102.5 and tick.security_id == 11536


def test_parse_message_handles_short_buffers_and_disconnect():
    assert ws.parse_header(b"\x00\x01") is None          # too short
    assert ws.parse_message(b"") is None
    disc = struct.pack(ws.HEADER_FMT, ws.CODE_DISCONNECT, 8, 1, 999)
    assert ws.parse_message(disc) == {"disconnect": True, "security_id": 999}
    # an unsupported code decodes header-only and returns None (honest skip)
    quote = struct.pack(ws.HEADER_FMT, ws.CODE_QUOTE, 8, 1, 5)
    assert ws.parse_message(quote) is None


def test_subscribe_frame_shape():
    frame = ws.build_subscribe_frame([(1, 11536), (1, 1333)], mode=ws.MODE_QUOTE)
    assert frame["RequestCode"] == 15 and frame["InstrumentCount"] == 2
    assert frame["Mode"] == "QUOTE"
    assert frame["InstrumentList"][0] == {"ExchangeSegment": 1,
                                          "SecurityId": "11536"}


def test_keepalive_timing_and_staleness():
    ka = ws.KeepAlive(interval=10.0, timeout=30.0)
    assert ka.due_for_ping(now=0.0) is True      # never pinged -> due
    ka.mark_ping(0.0)
    assert ka.due_for_ping(now=5.0) is False
    assert ka.due_for_ping(now=10.0) is True
    ka.note_rx(0.0)
    assert ka.is_stale(now=20.0) is False         # within timeout
    assert ka.is_stale(now=31.0) is True          # no rx past timeout


def test_backoff_schedule_grows_and_caps():
    assert [ws.backoff_schedule(i) for i in range(4)] == [1.0, 2.0, 4.0, 8.0]
    assert ws.backoff_schedule(20) == 30.0        # capped
    assert ws.backoff_schedule(-5) == 1.0         # clamps negatives


def test_handle_frame_fans_out_ticks():
    ticks = []
    client = ws.DhanFeedClient(token="x", on_tick=ticks.append)
    client.handle_frame(_ticker_packet(1333, 55.25), now=1.0)
    assert len(ticks) == 1 and ticks[0].last_price == 55.25
    assert client.keepalive._last_rx == 1.0       # rx noted for keep-alive
