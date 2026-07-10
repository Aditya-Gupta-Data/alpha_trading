"""
Tests for the Phase-0 VM telemetry on the ops health card. Offline —
synthetic /proc files in temp dirs.

Run either of these from the project folder:
    python tests/test_ops_telemetry.py
    python -m pytest tests/test_ops_telemetry.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import ops_monitor as om


_MEMINFO = """MemTotal:        1024000 kB
MemFree:          100000 kB
MemAvailable:     204800 kB
SwapTotal:        524288 kB
SwapFree:         262144 kB
"""


def test_telemetry_reads_synthetic_proc_files():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "meminfo").write_text(_MEMINFO)
        (tmp / "loadavg").write_text("0.42 0.30 0.20 1/123 4567\n")
        t = om.system_telemetry(meminfo_path=str(tmp / "meminfo"),
                                loadavg_path=str(tmp / "loadavg"),
                                disk_path=str(tmp))
        assert t["mem_total_mb"] == 1000
        assert t["mem_available_mb"] == 200
        assert t["mem_used_pct"] == 80          # 1 - 204800/1024000
        assert t["swap_total_mb"] == 512 and t["swap_used_mb"] == 256
        assert t["load_1m"] == 0.42
        assert t["disk_free_gb"] is not None and t["disk_used_pct"] is not None


def test_missing_proc_files_read_none_never_raise():
    t = om.system_telemetry(meminfo_path="/nonexistent/meminfo",
                            loadavg_path="/nonexistent/loadavg")
    assert t["mem_used_pct"] is None and t["load_1m"] is None
    line = om.telemetry_line(t)
    assert "?" in line          # absent readings render as '?', never faked


def test_card_carries_the_telemetry_line_in_both_shapes():
    t = {"mem_used_pct": 80, "mem_available_mb": 200, "swap_used_mb": 256,
         "load_1m": 0.42, "disk_free_gb": 5.5, "disk_used_pct": 45}
    clean = om.build_card([], [], "2026-07-10 20:30", telemetry=t)
    assert "✅" in clean and "mem 80% used" in clean
    dirty = om.build_card([{"log": "x.log", "line": "error: boom", "count": 1}],
                          ["main.log"], "2026-07-10 20:30", telemetry=t)
    assert "🩺" in dirty and "mem 80% used" in dirty
    # Legacy call without telemetry unchanged.
    assert "mem" not in om.build_card([], [], "2026-07-10 20:30")


def test_new_capture_jobs_are_heartbeat_monitored():
    for job in ("chain_archiver.log", "deals_tracker.log",
                "daily_archiver.log"):
        assert job in om.EXPECTED_JOBS


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed.")
