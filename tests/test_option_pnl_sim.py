"""Tests for src/research/option_pnl_sim.py. Hermetic: synthetic F&O day files in tmp_path, injected closes and VIX."""
from __future__ import annotations

import csv
import gzip
import math
from datetime import date, timedelta

from src.research import option_pnl_sim as ops

COLS = ("TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "StrkPric", "OptnTp", "OpnPric", "HghPric", "LwPric",
        "ClsPric", "SttlmPric", "UndrlygPric", "OpnIntrst", "TtlTradgVol", "NewBrdLotQty")
EXPIRY = "2024-02-29"


def _price(spot, k, kind):
    tv = 60 * math.exp(-abs(k - spot) / 182)
    return round((max(spot - k, 0) if kind == "CE" else max(k - spot, 0)) + tv, 2)


def _write_days(tmp_path, spots, traded=True):
    days = []
    d = date(2024, 2, 1)
    for spot in spots:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        rows = []
        for k in range(21000, 23050, 50):
            for kind in ("CE", "PE"):
                p = _price(spot, k, kind)
                rows.append({"TradDt": d.isoformat(), "FinInstrmTp": "IDO", "TckrSymb": "NIFTY", "XpryDt": EXPIRY,
                             "StrkPric": f"{k}.00", "OptnTp": kind, "OpnPric": p, "HghPric": p, "LwPric": p,
                             "ClsPric": p, "SttlmPric": p, "UndrlygPric": spot, "OpnIntrst": 100,
                             "TtlTradgVol": 10 if traded else 0, "NewBrdLotQty": 50})
        with gzip.open(tmp_path / f"{d.isoformat()}.csv.gz", "wt", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=COLS)
            w.writeheader()
            w.writerows(rows)
        days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def _closes(days, spots):
    hist = [((date(2023, 1, 2) + timedelta(days=i)).isoformat(), 18000 + 10 * i) for i in range(380)]
    return hist + list(zip(days, spots))


def test_a_bullish_view_trades_a_bull_call_spread_on_real_closes_and_exits_by_the_rules(tmp_path):
    spots = [22000 + 60 * i for i in range(21)]
    days = _write_days(tmp_path, spots)
    fo = ops.FoHistory(tmp_path)
    res = ops.simulate(days[0], days[-1], "live", symbols=("NIFTY",), fo=fo,
                       closes_by_sym={"NIFTY": _closes(days, spots)}, vix_by_day={d: 13.0 for d in days})
    t = res["trades"]
    assert t and t[0]["strategy"] == "bull_call_spread" and t[0]["entry"] == days[0]
    assert t[0]["pnl_rs"] > 0 and t[0]["r"] > 0 and t[0]["costs_rs"] > 0
    assert t[0]["resolution"] in ("ratchet_hit", "pre_expiry_exit", "expiry_last_mark")
    rep = ops.report(res)
    assert rep["ALL"]["n"] == len(t) and rep["by_strategy"]["bull_call_spread"]["win_rate"] == 1.0
    assert any("MODE live" in l for l in ops.lines(rep))


def test_untraded_strikes_are_not_a_chain(tmp_path):
    spots = [22000 + 60 * i for i in range(6)]
    days = _write_days(tmp_path, spots, traded=False)
    fo = ops.FoHistory(tmp_path)
    oc = ops.chain_for(fo.day(days[0])["NIFTY"], EXPIRY)["oc"]
    assert oc and all(leg["last_price"] == 0 for node in oc.values() for leg in node.values())
    res = ops.simulate(days[0], days[-1], "live", symbols=("NIFTY",), fo=fo,
                       closes_by_sym={"NIFTY": _closes(days, spots)}, vix_by_day={})
    assert res["trades"] == [] and sum(res["refusals"].values()) > 0


def test_skip_bearish_and_bull_only_modes(tmp_path):
    spots = [22800 - 60 * i for i in range(21)]
    days = _write_days(tmp_path, spots)
    fo = ops.FoHistory(tmp_path)
    falling = [((date(2023, 1, 2) + timedelta(days=i)).isoformat(), 26000 - 8 * i) for i in range(380)]
    closes = {"NIFTY": falling + list(zip(days, spots))}
    skip = ops.simulate(days[0], days[-1], "skip_bearish", symbols=("NIFTY",), fo=fo, closes_by_sym=dict(closes),
                        vix_by_day={})
    assert skip["trades"] == [] and skip["refusals"].get("skipped bearish view")
    live = ops.simulate(days[0], days[-1], "live", symbols=("NIFTY",), fo=fo, closes_by_sym=dict(closes), vix_by_day={})
    assert live["trades"] and live["trades"][0]["strategy"] == "bear_put_spread" and live["trades"][0]["r"] > 0
    bull = ops.simulate(days[0], days[-1], "bull_only", symbols=("NIFTY",), fo=fo, closes_by_sym=dict(closes),
                        vix_by_day={})
    assert bull["trades"] and bull["trades"][0]["strategy"] == "bull_call_spread" and bull["trades"][0]["r"] < 0


def test_summarize_drawdown_and_profit_factor():
    tr = [{"exit": "2024-01-02", "pnl_rs": 100.0, "r": 0.5, "costs_rs": 5.0},
          {"exit": "2024-01-03", "pnl_rs": -300.0, "r": -1.0, "costs_rs": 5.0},
          {"exit": "2024-01-04", "pnl_rs": 50.0, "r": 0.2, "costs_rs": 5.0}]
    s = ops.summarize(tr)
    assert s["n"] == 3 and s["max_drawdown_rs"] == -300.0 and s["profit_factor"] == 0.5 and s["total_rs"] == -150.0
    assert ops.summarize([]) == {"n": 0}


def test_a_nearly_free_spread_is_refused_and_open_positions_are_not_scored(tmp_path, monkeypatch):
    spots = [22000 + 60 * i for i in range(21)]
    days = _write_days(tmp_path, spots)
    fo = ops.FoHistory(tmp_path)
    kw = dict(symbols=("NIFTY",), fo=fo, closes_by_sym={"NIFTY": _closes(days, spots)}, vix_by_day={})
    monkeypatch.setattr(ops, "MAX_PLAUSIBLE_REWARD_RISK", 0.1)
    res = ops.simulate(days[0], days[-1], "live", **kw)
    assert res["trades"] == [] and res["refusals"].get("implausible close-price book (reward:risk > 9)")
    monkeypatch.setattr(ops, "MAX_PLAUSIBLE_REWARD_RISK", 9.0)
    short = ops.FoHistory(tmp_path)
    short.days = short.days[:4]
    res = ops.simulate(days[0], days[3], "live", symbols=("NIFTY",), fo=short,
                       closes_by_sym={"NIFTY": _closes(days, spots)}, vix_by_day={})
    assert all(t["resolution"] != "open_at_data_end" for t in res["trades"])
