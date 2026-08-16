"""Corporate-action adjuster (src/ingestion/corporate_actions.py) and its
bhavcopy_clerk wiring. Offline: synthetic bars, temp config, temp lake."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ingestion import corporate_actions as ca


def _bar(session, close, volume=1000.0, prev_close=None, open_=None):
    return {"session": session, "date": session, "open": open_ or close,
            "high": close * 1.01, "low": close * 0.99, "close": close,
            "prev_close": prev_close if prev_close is not None else close,
            "avg_price": close, "volume": volume, "deliv_qty": volume / 2}


# a 1:5 split on 2021-10-28: 4000-ish before, 800-ish after
BARS = [_bar("2021-10-26", 4000.0), _bar("2021-10-27", 4100.0),
        _bar("2021-10-28", 830.0, volume=12000.0, prev_close=4100.0, open_=820.0),
        _bar("2021-10-29", 845.0, volume=9000.0, prev_close=830.0)]
ACT = [{"symbol": "X", "ex_date": "2021-10-28", "ratio": 5.0}]


def test_history_before_the_ex_date_is_scaled_and_the_ex_date_onward_is_raw():
    out = ca.adjust_bars(BARS, ACT)
    assert [b["close"] for b in out] == [800.0, 820.0, 830.0, 845.0]
    assert [b["adj_factor"] for b in out] == [5.0, 5.0, 1.0, 1.0]
    assert out[0]["volume"] == 5000.0 and out[0]["raw_close"] == 4000.0
    assert out[-1] == {**BARS[-1], "raw_close": 845.0, "adj_factor": 1.0}
    assert BARS[0]["close"] == 4000.0                # input not mutated


def test_the_latest_bar_is_never_scaled_whatever_the_actions_say():
    """Execution/mark safety: the newest print is always the exchange's."""
    acts = ACT + [{"symbol": "X", "ex_date": "2021-10-29", "ratio": 2.0}]
    out = ca.adjust_bars(BARS, acts)
    assert out[-1]["close"] == 845.0 and out[-1]["adj_factor"] == 1.0
    assert out[0]["adj_factor"] == 10.0             # cumulative before both


def test_no_actions_means_the_same_prices_with_factor_one():
    out = ca.adjust_bars(BARS, [])
    assert [b["close"] for b in out] == [4000.0, 4100.0, 830.0, 845.0]
    assert all(b["adj_factor"] == 1.0 for b in out)


def test_config_rows_are_validated_and_half_rows_dropped(tmp_path):
    p = tmp_path / "ca.json"
    p.write_text(json.dumps({"actions": [
        {"symbol": "x", "ex_date": "2021-10-28", "ratio": "5"},
        {"symbol": "Y", "ex_date": "2022-01-01"},                # no ratio
        {"symbol": "Z", "ex_date": "2022-01-01", "ratio": 0},   # zero
        {"symbol": "", "ex_date": "2022-01-01", "ratio": 2}]}))
    acts = ca.load_actions(p)
    assert [(a["symbol"], a["ratio"]) for a in acts] == [("X", 5.0)]
    assert ca.actions_for("x.NS", path=p)[0]["ex_date"] == "2021-10-28"
    assert ca.load_actions(tmp_path / "missing.json") == []


def test_the_lake_detector_finds_the_split_and_ignores_an_ordinary_gap():
    c = ca.detect_candidates(BARS, "X")
    assert len(c) == 1 and c[0]["ex_date"] == "2021-10-28" and c[0]["ratio"] == 5.0
    assert c[0]["kind"] == "candidate" and c[0]["verified_against_nse_circular"] is False
    # a 12% gap-down on 3x volume is news, not a unit change
    news = [_bar("2026-01-05", 100.0), _bar("2026-01-06", 88.0, volume=3000.0,
                                             prev_close=100.0, open_=88.0)]
    assert ca.detect_candidates(news, "N") == []
    # a clean 5x gap WITHOUT a volume surge is not enough either
    quiet = [_bar("2026-01-05", 100.0), _bar("2026-01-06", 20.0, volume=1000.0,
                                              prev_close=100.0, open_=20.0)]
    assert ca.detect_candidates(quiet, "Q") == []


def test_config_wins_over_detection_and_auto_only_adds_when_asked(tmp_path):
    p = tmp_path / "ca.json"
    p.write_text(json.dumps({"actions": []}))
    raw = ca.adjusted("X", BARS, path=p)                 # config empty, no auto
    assert raw[0]["close"] == 4000.0
    auto = ca.adjusted("X", BARS, path=p, auto=True)     # lake evidence used
    assert auto[0]["close"] == 800.0
    p.write_text(json.dumps({"actions": [{"symbol": "X", "ex_date": "2021-10-28",
                                          "ratio": 4.0}]}))
    both = ca.adjusted("X", BARS, path=p, auto=True)     # same date: config's 4 wins
    assert both[0]["adj_factor"] == 4.0


def test_yfinance_cross_check_recovers_the_multiplier_from_adjusted_closes():
    # yfinance is split-adjusted: its closes read 800/820/830/845 throughout
    yf = {"2021-10-27": 820.0, "2021-10-28": 830.0}
    r = ca.yfinance_ratio("X", "2021-10-28", BARS, fetch_fn=lambda s: yf)
    assert r["ok"] and r["snapped_ratio"] == 5.0
    assert abs(r["multiplier"] - 4100.0 / 830.0 / (820.0 / 830.0)) < 1e-6
    bad = ca.yfinance_ratio("X", "2021-10-28", BARS,
                            fetch_fn=lambda s: (_ for _ in ()).throw(RuntimeError("no net")))
    assert bad["ok"] is False and "yfinance unavailable" in bad["reason"]


def test_bars_for_applies_confirmed_actions_by_default_and_raw_on_request(tmp_path):
    from src.ingestion import bhavcopy_clerk as bc
    lake = tmp_path / "lake"; lake.mkdir()
    hdr = "SYMBOL,SERIES,DATE1,PREV_CLOSE,OPEN_PRICE,HIGH_PRICE,LOW_PRICE,LAST_PRICE,CLOSE_PRICE,AVG_PRICE,TTL_TRD_QNTY,TURNOVER_LACS,NO_OF_TRADES,DELIV_QTY,DELIV_PER\n"
    def row(day, pc, o, c, v):
        return f"X,EQ,{day},{pc},{o},{c},{c},{c},{c},{c},{v},1,1,{v//2},50\n"
    (lake / "2021-10-27.csv").write_text(hdr + row("27-Oct-2021", 4000, 4100, 4100, 1000))
    (lake / "2021-10-28.csv").write_text(hdr + row("28-Oct-2021", 4100, 820, 830, 12000))
    (lake / "2021-10-29.csv").write_text(hdr + row("29-Oct-2021", 830, 840, 845, 9000))
    cfg = tmp_path / "ca.json"
    cfg.write_text(json.dumps({"actions": [{"symbol": "X", "ex_date": "2021-10-28", "ratio": 5}]}))
    adj = bc.bars_for("X", days=10, lake_dir=lake, actions_path=cfg)
    assert [b["close"] for b in adj] == [820.0, 830.0, 845.0]
    assert adj[-1]["close"] == 845.0 and adj[-1]["adj_factor"] == 1.0
    raw = bc.bars_for("X", days=10, lake_dir=lake, adjust=False)
    assert [b["close"] for b in raw] == [4100.0, 830.0, 845.0]
    many = bc.bars_for_many(["X"], days=10, lake_dir=lake, actions_path=cfg)
    assert [b["close"] for b in many["X"]] == [820.0, 830.0, 845.0]
    # an unreadable config is raw bars, never a crash
    assert [b["close"] for b in bc.bars_for("X", days=10, lake_dir=lake,
                                            actions_path=tmp_path / "nope.json")] == [4100.0, 830.0, 845.0]


def test_the_shipped_config_parses_and_every_row_states_its_provenance():
    acts = ca.load_actions()
    assert acts, "config/corporate_actions.json should ship with the measured rows"
    for a in acts:
        assert a["ratio"] >= 1.5 and a["source"] and "verified_against_nse_circular" in a
