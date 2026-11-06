"""Tests for scripts/backtest_filings.py (MANUAL OFFLINE TOOL). Hermetic: tmp_path out dir, fake NSE rows, fake bars."""
from __future__ import annotations

import importlib.util
import json
from datetime import date, timedelta
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "backtest_filings", Path(__file__).resolve().parents[1] / "scripts" / "backtest_filings.py")
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)

ORDER = {"seq_id": 1, "an_dt": "07-Mar-2025 22:59:41", "symbol": "HFCL", "sm_name": "HFCL Limited",
         "desc": "Bagging/Receiving of orders/contracts",
         "attchmntText": "HFCL Limited has informed the Exchange that Reliance Industries awarded HFCL a contract "
                         "worth INR 44.36 Crores for supply of optical fibre cable"}
BOILER = {"seq_id": 2, "an_dt": "07-Mar-2025 22:22:12", "symbol": "NAZARA", "desc": "Acquisition",
          "attchmntText": "Nazara Technologies Limited has informed the Exchange about Acquisition"}
AGM = {"seq_id": 3, "an_dt": "07-Mar-2025 10:00:00", "symbol": "X", "desc": "Shareholders meeting",
       "attchmntText": "X Limited has informed the Exchange about the outcome of the annual general meeting held today"}


TAKEOVER = {"seq_id": 4, "an_dt": "07-Mar-2025 11:00:00", "symbol": "GRASIM", "desc": "Disclosure under SEBI Takeover Regulations",
            "attchmntText": "Grasim Industries Limited has Submitted to the Exchange a copy of Disclosure under Regulation 31(4) "
                            "of the SEBI (Substantial Acquisition of Shares and Takeovers) Regulations"}


def test_keep_takes_real_order_text_only():
    assert bt.keep(TAKEOVER) is None
    assert not bt.substantive("Updates", TAKEOVER["attchmntText"])
    k = bt.keep(ORDER)
    assert k["id"] == "1" and k["symbol"] == "HFCL" and k["at"] == "2025-03-07T22:59:41+05:30"
    assert bt.keep(BOILER) is None and bt.keep(AGM) is None


def test_backfill_is_weekly_resumable_and_dedups(tmp_path):
    calls = []
    fetch = lambda a, b: calls.append(a) or [ORDER, BOILER, AGM]
    s = bt.backfill(2025, out_dir=tmp_path, fetch_fn=fetch, pause=0)
    assert s["weeks"] == 53 and s["kept"] == 1 and len(calls) == 53
    again = bt.backfill(2025, out_dir=tmp_path, fetch_fn=fetch, pause=0)
    assert again["weeks"] == 0 and len(calls) == 53
    assert len(bt.paths(2025, tmp_path)["filings"].read_text().splitlines()) == 1


def test_a_failed_week_is_retried_next_time(tmp_path):
    s = bt.backfill(2025, out_dir=tmp_path, fetch_fn=lambda a, b: [], pause=0)
    assert len(s["failed_weeks"]) == 53 and not bt.paths(2025, tmp_path)["done"].exists()


def test_items_ingest_grade_end_to_end(tmp_path):
    bt.backfill(2025, out_dir=tmp_path, fetch_fn=lambda a, b: [ORDER] if a == date(2025, 3, 5) else [], pause=0)
    assert bt.items(2025, tmp_path) == 1
    p = bt.paths(2025, tmp_path)
    text = json.loads(p["items"].read_text())["text"]
    edge = {"subject": "Reliance Industries", "subject_type": "company", "predicate": "drives", "object": "HFCL",
            "object_type": "company", "polarity": 1, "strength": 0.7,
            "quote": "Reliance Industries awarded HFCL a contract"}
    raw = tmp_path / "qwen3_8b.jsonl"
    raw.write_text(json.dumps({"id": "1", "raw": json.dumps({"edges": [edge, {"predicate": "likes"}]})}) + "\n"
                   + json.dumps({"id": "1", "raw": "not json"}) + "\n")
    st = bt.ingest(2025, raw, out_dir=tmp_path)
    assert st["written"] == 1 and st["rejected"] == {"invalid_predicate": 1} and st["json_ok"] == 1
    assert "Reliance Industries awarded HFCL" in text

    def bars(syms):
        out = {}
        for s in syms:
            px, rows = 100.0, []
            for i in range(30):
                d = (date(2025, 3, 7) + timedelta(days=i)).isoformat()
                rows.append({"session": d, "open": px, "close": px + 1})
                px += 1
            out[s] = rows
        return out
    reps = bt.grade(2025, out_dir=tmp_path, horizons=(1,), bars_loader=bars)
    v = reps[0]["verdicts"]["drives"]
    assert v["n"] == 1 and v["hit_rate"] == 1.0
    assert p["ledger"].exists() and "BACKTEST" in reps[0]["label"]
    f = reps[0]["verdicts"]["FILER_ORDER"]
    assert f["n"] == 1 and f["hit_rate"] == 1.0 and reps[0]["filer_calls"] == 1
    assert "BACKTEST" in p["ledger"].read_text()


def test_filer_calls_split_orders_from_mna(tmp_path):
    p = bt.paths(2025, tmp_path)
    p["filings"].parent.mkdir(parents=True, exist_ok=True)
    rows = [bt.keep(ORDER),
            {"id": "9", "at": "2025-05-02T10:00:00+05:30", "symbol": "CCL", "desc": "Acquisition",
             "text": "CCL Products (India) Limited has informed the Exchange about Agreement to acquire equity stake in a roaster"},
            {"id": "8", "at": "2025-05-02T10:00:00+05:30", "symbol": "Z", "desc": "Disclosure under SEBI Takeover Regulations",
             "text": "Z has submitted Regulation 31(4) Substantial Acquisition of Shares paperwork"}]
    p["filings"].write_text("".join(json.dumps(r) + "\n" for r in rows))
    calls = bt.filer_calls(2025, tmp_path)
    assert [(c["ticker"], c["relation"], c["sign"]) for c in calls] == [("HFCL", "FILER_ORDER", 1), ("CCL", "FILER_MNA", 1)]
