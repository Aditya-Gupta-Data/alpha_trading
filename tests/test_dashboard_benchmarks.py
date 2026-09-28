"""Decision #119 — the Compounding chart's passive benchmarks: built from the
lakes on the VM (no network), rebased to contributed capital on the epoch,
FD compounded daily. Hermetic fixtures in tmp_path."""
import json
from datetime import date, datetime

from src.dashboard import benchmarks as bm


def _lakes(tmp_path):
    macro = tmp_path / "macro"; macro.mkdir()
    (macro / "NIFTY.csv").write_text("date,value\n2026-08-06,24636.0\n2026-08-07,24570.65\n"
                                     "2026-08-08,24000.0\n2026-09-28,22780.25\n2026-09-29,\n")
    bhav = tmp_path / "bhavcopy"; bhav.mkdir()
    head = "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE\n"
    (bhav / "2026-08-06.csv").write_text(head + "GOLDBEES, EQ, 06-Aug-2026, 1, 1, 1, 1, 1, 122.00, 1\n")
    (bhav / "2026-08-07.csv").write_text(head + "NIFTYBEES, EQ, 07-Aug-2026, 1, 1, 1, 1, 1, 279.83, 1\n"
                                                "GOLDBEES, BE, 07-Aug-2026, 1, 1, 1, 1, 1, 999.0, 1\n"
                                                "GOLDBEES, EQ, 07-Aug-2026, 1, 1, 1, 1, 1, 123.40, 1\n")
    (bhav / "2026-09-28.csv").write_text(head + "GOLDBEES, EQ, 28-Sep-2026, 1, 1, 1, 1, 1, 121.17, 1\n")
    (bhav / "README.md").write_text("not a day")
    return macro, bhav


def test_build_reads_index_and_etf_closes_from_the_epoch_only(tmp_path):
    macro, bhav = _lakes(tmp_path)
    raw = bm.build(macro, bhav, now=datetime(2026, 9, 28, 22, 0))
    assert raw["epoch"] == "2026-08-07" and raw["notes"] == {}
    assert raw["series"]["nifty50"] == [["2026-08-07", 24570.65], ["2026-08-08", 24000.0], ["2026-09-28", 22780.25]]
    assert raw["series"]["gold"] == [["2026-08-07", 123.40], ["2026-09-28", 121.17]]      # EQ row, not BE
    out = bm.write(raw, tmp_path / "b.json")
    assert json.loads(out.read_text())["sources"]["gold"].startswith("GOLDBEES")


def test_missing_lakes_yield_empty_series_with_a_note_never_a_price(tmp_path):
    raw = bm.build(tmp_path / "nope", tmp_path / "nope2")
    assert raw["series"] == {"nifty50": [], "gold": []}
    assert set(raw["notes"]) == {"nifty50", "gold"}


def test_normalize_rebases_to_contributed_capital_and_compounds_the_fd_daily(tmp_path):
    macro, bhav = _lakes(tmp_path)
    raw = bm.build(macro, bhav)
    n = bm.normalize(raw, 1_000_000.0, until=date(2026, 8, 9))
    nifty = n["series"]["nifty50"]
    assert nifty[0] == {"ts": "2026-08-07T15:30:00", "value": 1_000_000.0}
    assert nifty[-1]["value"] == round(1_000_000 * 22780.25 / 24570.65, 2)               # 927,132.xx
    assert n["series"]["gold"][-1]["value"] == round(1_000_000 * 121.17 / 123.40, 2)
    fd = n["series"]["fd_7pct"]
    assert [p["ts"][:10] for p in fd] == ["2026-08-07", "2026-08-08", "2026-08-09"]
    assert fd[0]["value"] == 1_000_000.0 and fd[2]["value"] == round(1_000_000 * (1 + 0.07 / 365) ** 2, 2)
    assert n["base"] == 1_000_000.0 and n["notes"] == {}
    # no file at all: FD only, the two market lines noted
    m = bm.normalize(None, 1_000_000.0, until=date(2026, 8, 7))
    assert m["series"]["nifty50"] == [] and m["series"]["gold"] == [] and len(m["series"]["fd_7pct"]) == 1
    assert set(m["notes"]) == {"nifty50", "gold"}
    assert bm.fd_series(1.0, "2026-08-07", date(2026, 8, 6)) == []
