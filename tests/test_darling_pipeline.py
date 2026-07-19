"""
The Darling Pipeline Phase 1, fully offline:

  * Dept 1 `ingestion/nse_results.py` — exchange-filed results capture:
    NULL-honest normalization (strings/nulls/'-' -> floats-or-None), bank
    detection, lake write, outage codes, the never-crash loop.
  * Dept 8 `analysis/fundamental_screener.py` — the v1 pass rule (revenue
    up YoY + 4q profit streak + EPS not shrinking), banks judged on
    interest earned, insufficient-data honesty, the forensic trust gate
    (Issue-19 contaminated originals never read, .v2 preferred), and the
    darlings_queue.json contract the downloader listens to.
  * The queue hand-off: report_downloader reads darlings_queue.json first,
    legacy screening_queue.json as fallback.
"""
import json

from src.analysis import fundamental_screener as FS
from src.ingestion import nse_results as NR
from src.ingestion import report_downloader as RD


# --------------------------------------------------------- nse_results

RAW = {"resCmpData": [
    # newest first: 5 quarters, filed as strings in lakhs
    {"re_from_dt": "01-Oct-2024", "re_to_dt": "31-Dec-2024",
     "re_create_dt": "09-JAN-2025", "re_net_sale": "1000",
     "re_net_profit": None, "re_con_pro_loss": "120",
     "re_basic_eps_for_cont_dic_opr": "3.30", "re_debt_eqt_rat": "-",
     "re_face_val": "1", "re_pdup": "100"},
    {"re_net_sale": "950", "re_con_pro_loss": "110",
     "re_basic_eps_for_cont_dic_opr": "3.10"},
    {"re_net_sale": "930", "re_con_pro_loss": "105",
     "re_basic_eps_for_cont_dic_opr": "3.00"},
    {"re_net_sale": "910", "re_con_pro_loss": "100",
     "re_basic_eps_for_cont_dic_opr": "2.90"},
    {"re_net_sale": "900", "re_con_pro_loss": "95",
     "re_basic_eps_for_cont_dic_opr": "3.00"},
]}


def test_normalize_is_null_honest_and_typed():
    n = NR.normalize(RAW)
    assert n["is_bank"] is False
    p0 = n["periods"][0]
    assert p0["net_sale"] == 1000.0
    assert p0["net_profit"] is None          # filed null stays None
    assert p0["debt_equity"] is None         # '-' stays None, never 0
    assert p0["net_profit_consolidated"] == 120.0
    assert p0["face_value"] == 1.0 and p0["paidup_capital"] == 100.0
    assert NR.normalize({}) == {"is_bank": False, "periods": []}


def test_normalize_flags_banks_via_interest_earned():
    raw = {"resCmpData": [{"re_int_earned": "5000", "re_net_sale": None}]}
    assert NR.normalize(raw)["is_bank"] is True


def test_fetch_one_captures_and_handles_no_data(tmp_path):
    r = NR.fetch_one("TCS.NS", fetch_json_fn=lambda u: RAW,
                     out_dir=tmp_path, log_path=tmp_path / "o.jsonl",
                     sleep_fn=lambda s: None)
    assert r["status"] == "captured" and r["periods"] == 5
    saved = json.loads((tmp_path / "TCS.json").read_text())
    assert saved["symbol"] == "TCS" and len(saved["periods"]) == 5

    empty = NR.fetch_one("GHOST", fetch_json_fn=lambda u: {},
                         out_dir=tmp_path, log_path=tmp_path / "o.jsonl",
                         sleep_fn=lambda s: None)
    assert empty["status"] == "no_data"
    assert "NR-404" in (tmp_path / "o.jsonl").read_text()


def test_run_loop_survives_a_dead_api(tmp_path):
    def dead(url):
        raise ConnectionError("HTTP Error 403: no")

    out = NR.run(["A", "B"], fetch_json_fn=dead, out_dir=tmp_path,
                 log_path=tmp_path / "o.jsonl", sleep_fn=lambda s: None)
    assert out["attempted"] == 2 and out["summary"]["outage"] == 2


# ----------------------------------------------------- the darling screen

def _capture(rev=(1000, 950, 930, 910, 900), eps=(3.3, 3.1, 3.0, 2.9, 3.0),
             profit=(120, 110, 105, 100, 95), bank=False):
    periods = []
    for i in range(5):
        periods.append({"net_sale": None if bank else rev[i],
                        "interest_earned": rev[i] if bank else None,
                        "net_profit": None,
                        "net_profit_consolidated": profit[i],
                        "eps_basic": eps[i]})
    return {"is_bank": bank, "periods": periods}


def test_screen_passes_a_growing_profitable_name():
    o = FS.screen_one("GOOD", _capture(), lake_dir="/nonexistent")
    assert o["status"] == "pass"
    assert o["metrics"]["revenue_yoy"] > 0
    assert o["forensic"] is None             # honest absence


def test_screen_judges_banks_on_interest_earned():
    o = FS.screen_one("BANK", _capture(bank=True), lake_dir="/nonexistent")
    assert o["status"] == "pass" and o["metrics"]["is_bank"] is True


def test_screen_rejects_shrinkers_and_loss_makers():
    down = FS.screen_one("DOWN", _capture(rev=(800, 950, 930, 910, 900)),
                         lake_dir="/nonexistent")
    assert down["status"] == "rejected"
    lossy = FS.screen_one("LOSS", _capture(profit=(-5, 110, 105, 100, 95)),
                          lake_dir="/nonexistent")
    assert lossy["status"] == "rejected"


def test_screen_insufficient_data_never_guesses():
    cap = _capture()
    cap["periods"][4]["eps_basic"] = None    # can't judge EPS YoY
    o = FS.screen_one("THIN", cap, lake_dir="/nonexistent")
    assert o["status"] == "insufficient_data"


def _seed_forensic(root, sym, fy, score, reds=0, v2=False):
    d = root / sym
    d.mkdir(parents=True, exist_ok=True)
    name = f"{fy}.v2.json" if v2 else f"{fy}.json"
    (d / name).write_text(json.dumps(
        {"conviction_score": score, "red_flags": [{}] * reds,
         "hidden_debt_flags": []}))


def test_forensic_gate_rejects_flags_and_skips_contaminated(tmp_path):
    _seed_forensic(tmp_path, "BADBOOKS", "FY25", 30)
    o = FS.screen_one("BADBOOKS", _capture(), lake_dir=tmp_path)
    assert o["status"] == "rejected" and "forensic" in o["reason"]

    _seed_forensic(tmp_path, "CAUTION", "FY25", 55)
    assert FS.screen_one("CAUTION", _capture(),
                         lake_dir=tmp_path)["status"] == "flagged"

    # the Issue-19 contaminated original must be invisible; v2 wins
    _seed_forensic(tmp_path, "EMUDHRA", "FY26", 10)          # contaminated
    _seed_forensic(tmp_path, "EMUDHRA", "FY26", 55, v2=True)  # clean v2
    view = FS.forensic_view("EMUDHRA", lake_dir=tmp_path)
    assert view["score"] == 55


def test_run_writes_the_queue_with_all_passers(tmp_path):
    res = tmp_path / "results"
    res.mkdir()
    (res / "GOOD.json").write_text(json.dumps(_capture()))
    (res / "DOWN.json").write_text(json.dumps(
        _capture(rev=(800, 950, 930, 910, 900))))
    q = FS.run(results_dir=res, lake_dir=tmp_path / "lake",
               queue_path=tmp_path / "q.json")
    assert q["tickers"] == ["GOOD"]
    assert "DOWN" in q["rejected"]
    written = json.loads((tmp_path / "q.json").read_text())
    assert written["tickers"] == ["GOOD"]     # the downloader contract
    assert "criteria" in written


def test_downloader_reads_darlings_queue_then_legacy(tmp_path, monkeypatch):
    darlings = tmp_path / "darlings_queue.json"
    legacy = tmp_path / "screening_queue.json"
    monkeypatch.setattr(RD, "QUEUE_PATH", darlings)
    monkeypatch.setattr(RD, "LEGACY_QUEUE_PATH", legacy)
    legacy.write_text(json.dumps({"tickers": ["OLD"]}))
    assert RD.load_queue() == ["OLD"]         # fallback works
    darlings.write_text(json.dumps({"tickers": ["NEW"]}))
    assert RD.load_queue() == ["NEW"]         # canonical wins
