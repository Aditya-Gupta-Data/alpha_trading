"""scripts/audit_logs.py — hermetic: a synthetic logs/ + journal in tmp_path."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("audit_logs", ROOT / "scripts" / "audit_logs.py")
al = importlib.util.module_from_spec(spec)
spec.loader.exec_module(al)


def _box(tmp_path):
    logs = tmp_path / "logs"; logs.mkdir()
    (tmp_path / "data").mkdir()
    (logs / "main.log").write_text(
        "2026-09-10 09:20 [Market Loop] NIFTY 50: proposal fired\n"
        "2026-09-11 09:31 dhan quote failed: DH-905 Too many requests — retry after pause\n"
        "  second attempt ok\n"
        "2026-09-12 10:02 ReadTimeout: HTTPSConnectionPool timed out\n"
        "2026-09-13 09:15 DH-902 data API subscription lapsed\n"
        "2026-09-14 09:16 [Market Loop] NIFTY BANK: no market state (quote stale)\n")
    (logs / "ops_monitor.log").write_text(
        "2026-09-10 20:30 🖥 mem 60% used (380MB free) · swap 0MB · load 0.2 · disk 3GB free (60%)\n"
        "2026-09-11 20:30 🖥 mem 90% used (90MB free) · swap 120MB · load 2.5 · disk 3GB free (60%)\n"
        "2026-09-12 20:30 🖥 mem 91% used (85MB free) · swap 200MB · load 2.6 · disk 3GB free (60%)\n"
        "2026-09-13 20:30 🖥 mem 92% used (80MB free) · swap 300MB · load 0.3 · disk 3GB free (60%)\n")
    (logs / "problems.jsonl").write_text(json.dumps(
        {"log": "main.log", "line": "DH-905", "count": 3, "found": "2026-09-11T20:30:00"}) + "\n")
    spread = {"strategy": "bull_call_spread", "expiry": "2026-09-01", "lot_size": 10, "lots": 1,
              "max_loss": 60.0, "max_profit": 40.0, "entry_spot": 100.0,
              "legs": [{"side": "BUY", "option_type": "CE", "strike": 100.0, "premium": 10.0},
                       {"side": "SELL", "option_type": "CE", "strike": 110.0, "premium": 4.0}]}
    rows = [{"short_id": "dead0001", "ticker": "NIFTY 50", "date": "2026-08-20", "decision": "approved",
             "outcome": None, "spread": spread},
            {"short_id": "open0002", "ticker": "XYZ.NS", "date": "2026-09-10", "decision": "approved",
             "outcome": None, "spread": dict(spread, expiry="2026-10-28")},
            {"short_id": "res00003", "ticker": "NIFTY 50", "date": "2026-08-20", "decision": "approved",
             "outcome": {"exit_date": "2026-09-12", "settlement_basis": "no_price_data_max_loss"},
             "spread": spread}]
    (tmp_path / "data" / "journal.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return tmp_path


def test_report_grades_every_hunt(tmp_path, capsys):
    box = _box(tmp_path)
    out = box / "audit.md"
    assert al.main(["--root", str(box), "--out", str(out), "--days", "15", "--today", "2026-09-19"]) == 0
    text = out.read_text()
    assert text.startswith("# Forensic log sweep — 2026-09-04 → 2026-09-19")
    # api: DH-905 + retry = WARNING recovered; timeout WARNING; DH-902 CRITICAL
    assert "DH-902" in text and "auth / data-access failure" in text
    assert "recovered on retry" in text and "timeout" in text
    # stale: the market-loop line is a WARNING
    assert "no market state" in text and "HONEST LIMIT" in text
    # memory: 3 consecutive hot readings -> CRITICAL; load run of 2 -> WARNING; swap rising
    assert "3 CONSECUTIVE high-memory readings" in text
    assert "2 CONSECUTIVE high-load readings" in text
    assert "swap rising" in text
    # exits: expired-and-open CRITICAL; backstop settlement WARNING; unpriceable INFO
    assert "expired 2026-09-01 and is STILL OPEN" in text
    assert "settled at the DEFINED MAX LOSS" in text
    assert "cannot replay offline" in text
    # the ops ledger roll-up
    assert "ops monitor logged 3 problem line(s) from `main.log`" in text
    crit = text.split("## CRITICAL")[1].split("## WARNING")[0]
    assert "DH-902" in crit and "STILL OPEN" in crit and "high-memory" in crit


def test_empty_box_is_reported_as_absent_not_clean(tmp_path):
    out = tmp_path / "r.md"
    assert al.main(["--root", str(tmp_path), "--out", str(out), "--today", "2026-09-19"]) == 0
    text = out.read_text()
    assert "no logs/ directory here" in text and "absent here" in text
    assert "| CRITICAL | 0 |" in text


def test_tool_is_offline_and_off_cron():
    src = (ROOT / "scripts" / "audit_logs.py").read_text()
    assert src.splitlines()[1].startswith("# MANUAL OFFLINE TOOL")
    for forbidden in ("dhan_client", "requests.", "urllib.request", "fire_broadcast",
                      "journal.log(", "rewrite_all", "release_entry"):
        assert forbidden not in src, forbidden
    assert "audit_logs" not in (ROOT / "scripts" / "setup_cron.sh").read_text()
