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
                 "mine_edges.sh", "run_evolution.sh", "ollama_session.sh"):
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
                "src.ingestion.scrip_master", "src.analysis.weekly_recalibration"):
        assert job in src, job
    assert src.count("mac_auto_sync.sh >>") == 3                       # three sync slots
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
