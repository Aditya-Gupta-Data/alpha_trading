"""
scripts/purge_holiday_lake_data.py — delete the price data captured on NSE
holidays (ledger Issue 41). Hermetic: a tmp lake, no network.
"""
import importlib.util
import json
import tarfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("purge_holiday", ROOT / "scripts" / "purge_holiday_lake_data.py")
ph = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ph)

HOL, REAL = "2026-10-02", "2026-10-01"          # Gandhi Jayanti; a real session


def _lake(tmp_path) -> Path:
    lake = tmp_path / "data" / "lake"
    for day in (HOL, REAL):
        for rel in (f"intraday_15m/date={day}", f"darlings_daily/date={day}", f"chains/nifty/date={day}",
                    f"chains/tcs/date={day}", f"candles/nifty-50/date={day}", f"cross_asset/date={day}",
                    f"macro_daily/date={day}"):
            (lake / rel).mkdir(parents=True)
            (lake / rel / "part.jsonl").write_text(json.dumps({"day": day, "price": 1.0}) + "\n")
    rows = [{"as_of": REAL, "symbol": "TCS", "close": 2075.0}, {"as_of": HOL, "symbol": "TCS", "close": 2075.0},
            {"as_of": HOL, "symbol": "INFY", "close": 1500.0}]
    (lake / "pricer_journal.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows) + f"not json but mentions {HOL}\n")
    return lake


def test_the_plan_lists_only_session_price_data_on_the_holiday(tmp_path):
    lake = _lake(tmp_path)
    plan = ph.build_plan(lake, [HOL])
    assert sorted(d["path"] for d in plan["dirs"]) == [
        f"candles/nifty-50/date={HOL}", f"chains/nifty/date={HOL}", f"chains/tcs/date={HOL}",
        f"darlings_daily/date={HOL}", f"intraday_15m/date={HOL}"]
    assert plan["rows"] == {"pricer_journal.jsonl": {HOL: 2}} and plan["refusals"] == []
    assert (lake / f"intraday_15m/date={HOL}").is_dir()                      # a plan deletes nothing


def test_a_real_session_or_a_bad_date_is_refused(tmp_path):
    lake = _lake(tmp_path)
    assert "not a listed NSE holiday" in ph.build_plan(lake, [REAL])["refusals"][0]
    assert "not a date" in ph.build_plan(lake, ["yesterday"])["refusals"][0]
    out = ph.purge(lake, [HOL, REAL], tmp_path / "data" / "purged_holiday_data")
    assert not out["purged"] and "not a listed NSE holiday" in out["reason"]
    assert (lake / f"chains/nifty/date={HOL}").is_dir()                      # one bad date: nothing is deleted
    assert not (tmp_path / "data" / "purged_holiday_data").exists()


def test_purge_archives_first_then_deletes_only_the_holiday(tmp_path):
    lake = _lake(tmp_path)
    arch_dir = tmp_path / "data" / "purged_holiday_data"
    out = ph.purge(lake, [HOL], arch_dir, now=datetime(2026, 10, 5, 16, 0))
    assert out["purged"] and out["dirs"] == 5 and out["files"] == 5 and out["rows"] == 2
    assert out["left_dirs"] == 0 and out["left_rows"] == {}
    for rel in ("intraday_15m", "darlings_daily", "chains/nifty", "chains/tcs", "candles/nifty-50"):
        assert not (lake / rel / f"date={HOL}").exists()
        assert (lake / rel / f"date={REAL}" / "part.jsonl").is_file()        # the real session is untouched
    for rel in ("cross_asset", "macro_daily"):                               # not NSE session prices: kept
        assert (lake / rel / f"date={HOL}" / "part.jsonl").is_file()
    kept = (lake / "pricer_journal.jsonl").read_text().splitlines()
    assert kept == [json.dumps({"as_of": REAL, "symbol": "TCS", "close": 2075.0}), f"not json but mentions {HOL}"]
    # the archive sits OUTSIDE the lake and holds every deleted file, the removed rows and a manifest
    archive = Path(out["archive"])
    assert archive.parent == arch_dir and lake not in archive.parents
    with tarfile.open(archive) as tar:
        names = sorted(m.name for m in tar.getmembers() if m.isfile())
        removed = tar.extractfile("removed_rows/pricer_journal.jsonl").read().decode().splitlines()
    assert f"chains/nifty/date={HOL}/part.jsonl" in names and "MANIFEST.json" in names and len(names) == 7
    assert [json.loads(r)["symbol"] for r in removed] == ["TCS", "INFY"]
    # idempotent
    again = ph.purge(lake, [HOL], arch_dir)
    assert not again["purged"] and again["reason"] == "nothing to purge"
    assert len(list(arch_dir.iterdir())) == 1


def test_the_cli_is_a_dry_run_without_yes(tmp_path, capsys, monkeypatch):
    lake = _lake(tmp_path)
    monkeypatch.setattr("src.market_loop.is_market_open", lambda now=None: False)
    assert ph.main(["--dates", HOL, "--lake", str(lake)]) == 0
    assert "dry run" in capsys.readouterr().out and (lake / f"chains/nifty/date={HOL}").is_dir()
    monkeypatch.setattr("src.market_loop.is_market_open", lambda now=None: True)
    assert ph.main(["--dates", HOL, "--lake", str(lake), "--yes"]) == 1
    assert "market is open" in capsys.readouterr().out and (lake / f"chains/nifty/date={HOL}").is_dir()
