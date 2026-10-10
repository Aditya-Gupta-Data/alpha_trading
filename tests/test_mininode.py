"""
The home node (Linux Mini PC) takes over the Mac lane — decision #99,
2026-09-16. These tests pin the portability contract: one explicit
interpreter resolved by scripts/node_env.sh, a cron installer that refuses
the wrong hosts and never touches the token, and no Mac-only pin left as
the sole interpreter in the three home-node scripts.

Offline; shell scripts are only syntax-checked and grepped. Run:
    python -m pytest tests/test_mininode.py -q
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _sh(*args, env=None):
    return subprocess.run(["bash", *args], capture_output=True, text=True,
                          cwd=ROOT, env=env, timeout=30)


def test_every_home_node_script_parses():
    for name in ("node_env.sh", "setup_mininode_cron.sh", "mac_auto_sync.sh",
                 "mine_edges.sh", "run_evolution.sh", "ollama_session.sh",
                 "node_preflight.sh"):
        r = _sh("-n", str(SCRIPTS / name))
        assert r.returncode == 0, f"{name}: {r.stderr}"


def test_node_env_resolves_one_explicit_interpreter_and_widens_path():
    r = _sh("-c", f". {SCRIPTS / 'node_env.sh'}; echo \"$PY|$PY_SOURCE|$CLOUDSDK_PYTHON\"; node_env_describe")
    assert r.returncode == 0, r.stderr
    py, source, cloudsdk = r.stdout.splitlines()[0].split("|")
    assert py and os.path.isabs(py), py                      # never a bare python3
    assert source in ("ALPHA_PY", "repo venv", "mac framework", "PATH fallback")
    assert cloudsdk == py                                    # gcloud pinned to ours
    assert "host=" in r.stdout and "os=" in r.stdout


def test_node_env_honours_an_explicit_alpha_py(tmp_path):
    fake = tmp_path / "py"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    env = dict(os.environ, ALPHA_PY=str(fake))
    env.pop("CLOUDSDK_PYTHON", None)
    r = _sh("-c", f". {SCRIPTS / 'node_env.sh'}; echo \"$PY|$PY_SOURCE\"", env=env)
    assert r.stdout.strip() == f"{fake}|ALPHA_PY"


def test_the_three_mac_scripts_now_source_node_env_and_carry_no_lone_pin():
    for name in ("mac_auto_sync.sh", "mine_edges.sh", "run_evolution.sh"):
        src = (SCRIPTS / name).read_text()
        assert "node_env.sh" in src, name
        # the framework path may appear only inside node_env.sh's fallback
        # ladder, never as this script's own interpreter
        assert "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m" not in src, name
        assert '"$PY"' in src, name
    assert "command -v ollama" in (SCRIPTS / "ollama_session.sh").read_text()


def test_the_node_installer_refuses_macos_the_vm_and_a_non_ist_clock():
    src = (SCRIPTS / "setup_mininode_cron.sh").read_text()
    assert 'uname -s)" = "Darwin"' in src
    assert "alpha-trading-vm*" in src
    assert '"+0530"' in src and "Asia/Kolkata" in src
    # on this Mac it must refuse before touching crontab
    r = _sh(str(SCRIPTS / "setup_mininode_cron.sh"), "--dry-run")
    if os.uname().sysname == "Darwin":
        assert r.returncode == 1 and "LINUX home node" in r.stderr


def test_the_node_schedule_is_the_mac_lane_and_nothing_of_the_vm_or_the_token():
    src = (SCRIPTS / "setup_mininode_cron.sh").read_text()
    for job in ("mac_auto_sync.sh", "mine_edges.sh", "run_evolution.sh",
                "src.ingestion.scrip_master"):
        assert job in src, job
    # three scheduled sync slots + ONE @reboot catch-up (10-10); the miner
    # gets the same catch-up because it self-gates
    assert src.count("mac_auto_sync.sh >>") == 4
    assert src.count("@reboot sleep") == 2                  # the two cron lines (comments aside)
    # Architect ruling E3 (10-09): recalibration is VM cron #36 — a node
    # copy would recalibrate on stale data every Saturday
    assert 'src.analysis.weekly_recalibration >>' not in src
    assert "-m src.analysis.weekly_recalibration" not in src
    for forbidden in ("renew_token", "push_token_to_vm", "setup_cron.sh\n", "master_scheduler",
                      "plan_tracker", "ops_monitor", "run_proving_court", "intraday_tracker"):
        assert forbidden not in src.replace("scripts/setup_cron.sh)", "").replace(
            "scripts/setup_cron.sh.", "").replace("scripts/setup_cron.sh is", ""), forbidden
    assert "BLOCK START" in src and "BLOCK END" in src                 # replace, never duplicate


def test_gcloud_path_resolution_falls_back_to_the_path_when_the_pin_is_absent(tmp_path, monkeypatch):
    from src import config
    fake = tmp_path / "gcloud"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert config._resolve_gcloud("/nonexistent/gcloud") == str(fake)
    assert config._resolve_gcloud(str(fake)) == str(fake)              # a real pin wins
    # an honest miss: nothing on PATH or in the widened ladder -> the
    # configured path comes back unchanged (never a bare "gcloud")
    import shutil
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    assert config._resolve_gcloud("/nonexistent/gcloud") == "/nonexistent/gcloud"


# ----------------------------------------------------------------------
# SHADOW TRIAL (2026-10-10, decision #143): the node runs a full parallel
# week beside the Mac and writes NOTHING to the VM or Discord.
# ----------------------------------------------------------------------

def _dry_run(*flags):
    env = dict(os.environ, TZ="Asia/Kolkata")
    return _sh(str(SCRIPTS / "setup_mininode_cron.sh"), "--dry-run", *flags, env=env)


def test_shadow_install_carries_the_switch_and_silences_the_scrip_card():
    if os.uname().sysname == "Darwin":
        return                                  # the installer refuses macOS by design
    r = _dry_run("--shadow")
    assert r.returncode == 0, r.stderr
    assert "ALPHA_NODE_SHADOW=1" in r.stdout
    assert "scrip_master --quiet" in r.stdout
    assert "SHADOW TRIAL" in r.stdout
    live = _dry_run()
    assert live.returncode == 0, live.stderr
    assert "ALPHA_NODE_SHADOW=0" in live.stdout
    assert "--quiet" not in live.stdout
    for out in (r.stdout, live.stdout):
        assert "weekly_recalibration" not in out.split("# 4.")[1].splitlines()[1]
        assert out.count("@reboot") == 2
        assert "@reboot sleep 90 && cd" in out and "mac_auto_sync.sh" in out


def test_the_sync_script_skips_the_ship_under_shadow_and_names_it():
    src = (SCRIPTS / "mac_auto_sync.sh").read_text()
    assert 'SHADOW="${ALPHA_NODE_SHADOW:-0}"' in src
    assert "SHADOW: would have shipped" in src
    assert "vm_push_file(p)" in src              # the real ship is still there for LIVE
    # under shadow the push is never reached: the `continue` sits before it
    ship = src.split("for art in MANIFEST:")[1].split("PYEOF")[0]
    assert ship.index("if SHADOW:") < ship.index("vm_push_file(p)")


def test_the_miner_wrapper_passes_no_apply_under_shadow():
    src = (SCRIPTS / "mine_edges.sh").read_text()
    assert "--no-apply" in src and "ALPHA_NODE_SHADOW" in src
    # and it still runs the miner with NO extra flag when the switch is off
    r = _sh("-c", "ALPHA_NODE_SHADOW=0; _MINER_ARGS=(); "
                  "[ \"${ALPHA_NODE_SHADOW:-0}\" = \"1\" ] && _MINER_ARGS+=(--no-apply); "
                  "echo \"x ${_MINER_ARGS[@]+\"${_MINER_ARGS[@]}\"} y\"")
    assert r.stdout.strip() == "x  y"


# ----------------------------------------------------------------------
# the week-end report
# ----------------------------------------------------------------------

_SYNC_LOG = """\
[2026-10-13 07:30:02] === mac auto-sync starting (py: /x/venv/bin/python) [SHADOW TRIAL — ships nothing] ===
[2026-10-13 07:30:40] sector bars: ok
[2026-10-13 07:31:10] valuation: ok
[2026-10-13 07:31:50] fo bhavcopy: ok
[2026-10-13 07:31:50] darling ids: ok
[2026-10-13 07:31:50] SHADOW: ship skipped
SHADOW: would have shipped 7/7: all seven
pulled 1/1: docs/LIVE_TRADE_BOOK.md
[2026-10-13 07:31:55] === mac auto-sync done ===
[2026-10-13 12:30:01] skip: last sync 299m ago (< 180m). --force overrides.
[2026-10-13 19:20:01] === mac auto-sync starting (py: /x/venv/bin/python) [SHADOW TRIAL — ships nothing] ===
[2026-10-13 19:20:30] sector bars: FAILED (keeping the stored file)
[2026-10-13 19:21:00] valuation: ok
[2026-10-13 19:21:30] fo bhavcopy: ok
[2026-10-13 19:21:30] darling ids: ok
SHADOW: would have shipped 6/7: six
NOT shipped: bars_cache.json:absent
pulled 0/1: none
[2026-10-13 19:21:33] === mac auto-sync done ===
[2026-10-14 09:02:11] === mac auto-sync starting (py: /x/venv/bin/python) [SHADOW TRIAL — ships nothing] ===
[2026-10-14 09:03:30] darling ids: ok
SHADOW: would have shipped 7/7: all
pulled 1/1: docs/LIVE_TRADE_BOOK.md
[2026-10-14 09:03:33] === mac auto-sync done ===
"""

_MINER_LOG = """\
[2026-10-13 21:00:40] edge_miner: {"status": "ok", "new_edges_applied_to_vm": 0, "shadow": true, "new_edges_mined_not_applied": 2}
[2026-10-14 21:00:12] edge_miner: {"status": "skipped", "reason": "Ollama not running"}
"""


def _report_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("node_trial_report", SCRIPTS / "node_trial_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_trial_report_parses_slots_throttles_failures_and_shadow_ships():
    from datetime import date
    m = _report_module()
    runs = m.parse_sync_log(_SYNC_LOG)
    assert [r["status"] for r in runs] == ["done", "throttled", "done", "done"]
    assert runs[0]["shadow"] is True and runs[0]["ship"] == 7 and runs[0]["pull"] == (1, 1)
    assert runs[2]["producers"]["sector bars"] == "FAILED" and runs[2]["ship"] == 6
    assert runs[2]["pull"] == (0, 1)
    miners = m.parse_miner_log(_MINER_LOG)
    assert miners[0]["summary"]["shadow"] is True and miners[1]["summary"]["status"] == "skipped"

    rep = m.build_report(runs, miners, boots=["2026-10-14 08:59"], days=2, today=date(2026, 10, 14))
    d13, d14 = rep["rows"]
    assert d13["slots_fired"] == ["07:30", "12:30", "19:20"]       # the throttle skip still counts as fired
    assert d13["producer_failures"] == ["sector bars"]
    assert d13["ship"] == 7 and d13["ship_worst"] == 6 and d13["pull_ok"] is True
    assert d14["slots_fired"] == [] and d14["extra_runs"] == 1       # the @reboot catch-up
    assert d14["boots"] == ["2026-10-14 08:59"] and d14["miner"] == "skipped"
    assert rep["verdict"] == "NOT YET"
    joined = "\n".join(rep["misses"])
    assert "producer FAILED — sector bars" in joined
    assert "2026-10-14 (Wed): slot(s) 07:30, 12:30, 19:20 never fired" in joined
    assert "one run would have shipped only 6/7" in joined
    text = m.render(rep)
    assert "mode: SHADOW" in text and "VERDICT: NOT YET" in text


def test_trial_report_calls_a_clean_week_reliable_and_a_missing_log_not_installed(tmp_path, capsys):
    from datetime import date
    m = _report_module()
    clean = []
    for day in range(13, 20):
        for hhmm in ("07:30", "12:30", "19:20"):
            clean.append(f"[2026-10-{day} {hhmm}:01] === mac auto-sync starting (py: /x) [SHADOW TRIAL — ships nothing] ===")
            clean += [f"[2026-10-{day} {hhmm}:30] {p}: ok" for p in m.PRODUCERS]
            clean.append("SHADOW: would have shipped 7/7: all")
            clean.append("pulled 1/1: docs/LIVE_TRADE_BOOK.md")
            clean.append(f"[2026-10-{day} {hhmm}:59] === mac auto-sync done ===")
    rep = m.build_report(m.parse_sync_log("\n".join(clean)), [], boots=[], days=7, today=date(2026, 10, 19))
    assert rep["verdict"] == "RELIABLE" and rep["clean_days"] == 7 and rep["misses"] == []
    assert "promote the node" in m.render(rep)

    # the CLI against an empty logs dir names the real cause
    logs = tmp_path / "logs"
    logs.mkdir()
    assert m.main(["--logs", str(logs), "--days", "2", "--through", "2026-10-14", "--no-boots"]) == 0
    out = capsys.readouterr().out
    assert "has the cron block been installed?" in out and "VERDICT: NOT YET" in out
    # and a populated one renders the table
    (logs / "mac_auto_sync.cron.log").write_text(_SYNC_LOG)
    (logs / "edge_miner.log").write_text(_MINER_LOG)
    assert m.main(["--logs", str(logs), "--days", "2", "--through", "2026-10-14", "--no-boots"]) == 0
    out = capsys.readouterr().out
    assert "2026-10-13 Tue" in out and "✓ ✓ ✓" in out and "FAIL:sector" in out


def test_preflight_is_read_only_and_names_every_fix(tmp_path):
    """scripts/node_preflight.sh (10-10): PASS/WARN/FAIL per prerequisite,
    exit 1 on any FAIL, writes nothing, never touches a token."""
    src = (SCRIPTS / "node_preflight.sh").read_text()
    # it may GREP for the token jobs (to flag them) but never invokes them,
    # never writes a file and never replaces the crontab
    for forbidden in ("src.renew_token", "push_token_to_vm.sh", "| crontab -", "> .env", ">> "):
        assert forbidden not in src, forbidden
    for check in ("+0530", "sleep.target", "is-active cron", "repo venv", "financial_results",
                  "DHAN_PIN|DHAN_TOTP_SECRET|DHAN_API_KEY|DHAN_API_SECRET", "auth list",
                  "project-37632031-10d0-47dd-b6f", "weekly_recalibration", "--no-vm"):
        assert check in src, check
    env = dict(os.environ, TZ="Asia/Kolkata", HOME=str(tmp_path))
    r = subprocess.run(["bash", str(SCRIPTS / "node_preflight.sh"), "--no-vm"],
                       capture_output=True, text=True, cwd=ROOT, env=env, timeout=120)
    assert "HOME NODE PREFLIGHT" in r.stdout and "SUMMARY:" in r.stdout
    assert r.returncode in (0, 1)
    if "NOT READY" in r.stdout:
        assert r.returncode == 1 and "  FAIL  " in r.stdout
    else:
        assert r.returncode == 0 and "READY." in r.stdout
    assert "VM round-trip skipped" in r.stdout            # --no-vm honoured: no ssh attempted
