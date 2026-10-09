"""Audit Chunk 5, Batch C: Q7 undated flows, F3 slot clamp, F6 disk alarm + rotate script, F9 ssh timeout."""
import subprocess
from pathlib import Path
from src import dhan_client as dc, ops_monitor as om

ROOT = Path(__file__).resolve().parent.parent


def test_f3_a_queued_slot_is_not_read_as_corrupt(tmp_path):
    f = tmp_path / "g"; f.write_text(str(1000.0 + 7 * dc._RATE_PAUSE))
    with f.open("r+") as h:
        assert dc._read_slot(h, 1000.0) == 1000.0 + 7 * dc._RATE_PAUSE      # 7 callers deep: kept
    f.write_text(str(1000.0 + 500.0))
    with f.open("r+") as h:
        assert dc._read_slot(h, 1000.0) == 1000.0                             # a clock jump: healed


def test_f6_disk_alarm_and_rotate_script(tmp_path):
    assert om.disk_alarm({"disk_free_gb": 0.4})["red"] and om.disk_alarm({"disk_free_gb": 3.0}) is None
    assert om.disk_alarm({}) is None
    logs = tmp_path / "logs"; logs.mkdir()
    big = logs / "a.log"; big.write_bytes(b"x" * (2 * 1048576)); (logs / "small.log").write_text("ok")
    (tmp_path / "scripts").mkdir(); script = tmp_path / "scripts" / "rotate_logs.sh"
    script.write_text((ROOT / "scripts" / "rotate_logs.sh").read_text())
    out = subprocess.run(["bash", str(script)], env={"ROTATE_MAX_MB": "1", "ROTATE_KEEP_MB": "1", "PATH": "/usr/bin:/bin"},
                         capture_output=True, text=True)
    assert "a.log" in out.stdout and big.stat().st_size == 1048576 and (logs / "small.log").read_text() == "ok"


def test_f9_cron_and_mirror_push_hygiene():
    cron = (ROOT / "scripts" / "setup_cron.sh").read_text()
    assert "rotate_logs.sh" in cron
    push = (ROOT / "scripts" / "publish_dashboard_mirror.sh").read_text()
    assert "timeout 30 ssh" in push and "ServerAliveInterval" in push


def test_q7_an_undated_flows_payload_never_writes_a_lake_row(tmp_path):
    import json
    from datetime import date
    from src.ingestion import flows_tracker as ft
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps([{"category": "FII/FPI", "buyValue": "1", "sellValue": "2", "netValue": "-1"}]))
    out = ft.run(use_live=False, snapshot_path=snap, output_path=tmp_path / "o.json",
                 lake_root=tmp_path / "lake", today=date(2026, 10, 9))
    assert out.get("undated") is True
    assert not (tmp_path / "lake" / "flows").exists()
