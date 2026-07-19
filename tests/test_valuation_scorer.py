"""
The Valuation Normalization Engine, fully offline: TTM metric math
(P/E, PEG, P/S from filed EPS/revenue/paid-up capital), the owner's
veto amendments (negative earnings / non-positive growth -> instant
veto, never capped), winsorized universe stats, sector-vs-market
fallback, sigmoid directionality (expensive scores HIGH — spec scale:
1 = deeply undervalued), and the end-to-end run() enrichment write.
"""
import json

from src.analysis import valuation_scorer as VS


def _capture(eps=(10.0, 9.0, 8.5, 8.0, 8.0), sales=1000.0,
             face=1.0, paidup=100.0, bank=False):
    periods = []
    for i in range(5):
        periods.append({"eps_basic": eps[i],
                        "net_sale": None if bank else sales,
                        "interest_earned": sales if bank else None,
                        "face_value": face, "paidup_capital": paidup})
    return {"is_bank": bank, "periods": periods}


def test_ttm_metrics_math():
    m = VS.ttm_metrics(_capture(), close=355.0)
    assert m["status"] == "ok"
    assert m["pe"] == 10.0                    # 355 / (10+9+8.5+8)
    assert m["peg"] == 0.4                    # 10 / 25% growth
    assert m["ps"] == 8.875                   # 355*100 / 4000


def test_veto_amendments_never_cap():
    neg = VS.ttm_metrics(_capture(eps=(-1.0, 2.0, 2.0, 2.0, 2.0)),
                         close=100.0)
    assert neg["status"] == "veto" and "EPS" in neg["reason"]
    flat = VS.ttm_metrics(_capture(eps=(8.0, 9.0, 8.5, 8.0, 8.0)),
                          close=100.0)
    assert flat["status"] == "veto"           # zero growth -> veto


def test_insufficient_data_never_guesses():
    thin = VS.ttm_metrics(_capture(), close=None)
    assert thin["status"] == "insufficient_data"
    cap = _capture()
    cap["periods"][0]["paidup_capital"] = None    # P/S uncomputable
    assert VS.ttm_metrics(cap, close=100.0)["status"] == "insufficient_data"


def test_winsorized_stats_trim_and_abstain():
    vals = [8.0 + i * 0.5 for i in range(30)] + [1000.0]   # one monster
    mu, sigma = VS.winsorized_stats(vals)
    assert mu < 50                            # outlier trimmed, not ruling
    assert sigma > 0
    assert VS.winsorized_stats([1.0, 2.0]) is None    # honest abstain
    assert VS.winsorized_stats([5.0] * 40) is None    # zero variance


def test_sector_stats_need_min_members_else_market():
    metrics = {f"S{i}": {"status": "ok", "pe": 10.0 + i, "peg": 1.0,
                         "ps": 2.0} for i in range(30)}
    small_sector = {f"S{i}": "TINY" for i in range(3)}    # < MIN_SECTOR_N
    stats = VS.build_stats(metrics, small_sector)
    assert "TINY" not in stats["sectors"]
    big_sector = {f"S{i}": "BIG" for i in range(25)}
    stats2 = VS.build_stats(metrics, big_sector)
    assert "BIG" in stats2["sectors"]


def test_sigmoid_directionality_expensive_scores_high():
    metrics = {f"S{i}": {"status": "ok", "pe": 10.0 + i * 0.7,
                         "peg": 0.5 + i * 0.05, "ps": 1.0 + i * 0.2}
               for i in range(30)}
    metrics["CHEAP"] = {"status": "ok", "pe": 5.0, "peg": 0.3, "ps": 0.8}
    metrics["RICH"] = {"status": "ok", "pe": 80.0, "peg": 4.0, "ps": 12.0}
    stats = VS.build_stats(metrics, {})
    cheap = VS.score_one(metrics["CHEAP"], stats)
    rich = VS.score_one(metrics["RICH"], stats)
    typical = VS.score_one(metrics["S15"], stats)
    assert cheap["score"] < typical["score"] < rich["score"]
    assert 1 <= cheap["score"] and rich["score"] <= 100
    assert cheap["stats_basis"] == "market"


def test_run_scores_only_queued_darlings(tmp_path):
    res = tmp_path / "results"
    res.mkdir()
    for i in range(25):
        (res / f"U{i}.json").write_text(json.dumps(
            _capture(eps=(10.0 + i * 0.3, 9.0, 8.5, 8.0, 7.5 - i * 0.05))))
    (res / "DARLING.json").write_text(json.dumps(_capture()))
    (res / "LOSSY.json").write_text(json.dumps(
        _capture(eps=(-1.0, 2.0, 2.0, 2.0, 2.0))))
    q = tmp_path / "q.json"
    q.write_text(json.dumps({"tickers": ["DARLING", "LOSSY", "UNKNOWN"]}))
    out = VS.run(results_dir=res, queue_path=q,
                 out_path=tmp_path / "val.json",
                 closes={f"U{i}": 300.0 + i * 15 for i in range(25)}
                 | {"DARLING": 355.0, "LOSSY": 100.0},
                 sector_map={})
    assert "DARLING" in out["scores"]
    assert out["vetoed"]["LOSSY"]
    assert "UNKNOWN" in out["insufficient_data"]
    written = json.loads((tmp_path / "val.json").read_text())
    assert written["universe_n"] >= 26
    assert "ADVISORY-ONLY" in written["advisory_note"]
