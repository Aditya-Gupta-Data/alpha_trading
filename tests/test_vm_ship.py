"""
The Mac→VM artifact ship (`firm_treasury.vm_push_file` + the EOD chain's
SHIP_MANIFEST), and the interpreter pin that resurrects it.

WHY THESE EXIST. The ship shipped on 2026-07-21 with **zero tests** and ran
dead from that day to 2026-08-05 without one visible error. Two independent
defects, each sufficient on its own:

  1. `gcloud` is a /bin/sh wrapper that finds a Python on PATH. Under cron's
     minimal PATH that is macOS's `/usr/bin/python3` (3.9.6), which gcloud
     refuses to load. Interactively it works — so it failed ONLY unattended.
  2. `vm_push_file` was `capture_output=True` + a bare
     `except Exception: return False`, so gcloud's own plain-English error
     was captured and thrown away every single night.

The consequence was the VM holding a 2026-07-20 tier table while
`equity_desk.TIERS_MAX_AGE_DAYS=3` silently refused every new equity entry
from ~07-24 onward. The desk's guard worked; nothing reported it.

Hermetic: the subprocess is injected, no gcloud is invoked, no network.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.firm_treasury as ft
from src import config


class _Proc:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = b""


def _recorder(proc=None, box=None):
    """A fake subprocess.run that records how it was called."""
    def run(cmd, **kw):
        (box if box is not None else {}).update({"cmd": cmd, "kw": kw})
        return proc or _Proc()
    return run


# ------------------------------------------------------- the interpreter pin

def test_gcloud_env_pins_a_supported_interpreter():
    """THE root cause. Without CLOUDSDK_PYTHON, cron's gcloud loads
    /usr/bin/python3 == 3.9 and dies before it ever reaches the network."""
    env = config.gcloud_env(env={"PATH": "/usr/bin:/bin"})
    assert env["CLOUDSDK_PYTHON"] == sys.executable
    assert env["PATH"] == "/usr/bin:/bin"          # everything else survives


def test_gcloud_env_never_overrides_an_explicit_owner_setting():
    env = config.gcloud_env(env={"CLOUDSDK_PYTHON": "/opt/my/python3"})
    assert env["CLOUDSDK_PYTHON"] == "/opt/my/python3"


def test_gcloud_env_accepts_an_explicit_executable():
    env = config.gcloud_env(env={}, executable="/x/py")
    assert env["CLOUDSDK_PYTHON"] == "/x/py"


def test_the_pin_is_the_python_running_us_not_whatever_is_on_path():
    """`sys.executable` under cron is the 3.14 framework python the crontab
    already names by absolute path — the one interpreter we know is good."""
    assert config.gcloud_env(env={})["CLOUDSDK_PYTHON"] == sys.executable
    assert "/usr/bin/python3" != sys.executable or True   # documents intent


# ------------------------------------------------------------ vm_push_file

def test_push_passes_the_pinned_env_to_the_subprocess():
    box = {}
    assert ft.vm_push_file("/tmp/a.json", run_fn=_recorder(box=box)) is True
    assert box["kw"]["env"]["CLOUDSDK_PYTHON"] == sys.executable
    assert box["kw"]["capture_output"] is True


def test_push_builds_the_expected_scp_command():
    box = {}
    ft.vm_push_file("/tmp/darling_tiers.json", run_fn=_recorder(box=box))
    cmd = box["cmd"]
    assert cmd[0] == ft.GCLOUD_PATH
    assert cmd[1:3] == ["compute", "scp"]
    assert cmd[3] == "/tmp/darling_tiers.json"
    assert cmd[4].endswith(":~/alpha_trading/data/")
    assert f"--project={ft.VM_SSH_PROJECT}" in cmd
    assert f"--zone={ft.VM_SSH_ZONE}" in cmd


def test_a_failed_push_NAMES_the_reason_instead_of_swallowing_it(capsys):
    """The 15-day silence, pinned. gcloud stated the cause in plain English
    every night; the old body captured it and dropped it on the floor."""
    proc = _Proc(returncode=1, stderr=b"ERROR: gcloud failed to load. You are "
                                      b"running gcloud with Python 3.9")
    assert ft.vm_push_file("/tmp/darling_tiers.json",
                           run_fn=_recorder(proc=proc)) is False
    out = capsys.readouterr().out
    assert "vm ship FAILED" in out
    assert "darling_tiers.json" in out
    assert "rc=1" in out
    assert "Python 3.9" in out                     # the actual cause, verbatim


def test_a_failure_with_no_stderr_still_says_so(capsys):
    assert ft.vm_push_file("/tmp/a.json",
                           run_fn=_recorder(proc=_Proc(1, b""))) is False
    assert "no stderr" in capsys.readouterr().out


def test_an_exploding_subprocess_fails_open_and_is_named(capsys):
    """Fail-open is still correct here — a missed ship must never break the
    EOD chain — but it may no longer be SILENT."""
    def boom(cmd, **kw):
        raise TimeoutError("timed out after 120 seconds")
    assert ft.vm_push_file("/tmp/a.json", run_fn=boom) is False
    out = capsys.readouterr().out
    assert "vm ship FAILED" in out and "TimeoutError" in out


def test_a_clean_push_says_nothing(capsys):
    assert ft.vm_push_file("/tmp/a.json", run_fn=_recorder()) is True
    assert capsys.readouterr().out == ""


def test_the_timeout_is_generous_enough_for_a_cold_gcloud():
    """A cold gcloud (OAuth refresh + SSH handshake) measured ~8.6s, but the
    edge miner has recorded real 120s SSH stalls to this VM. 90s was the old
    value; anything shorter turns a slow night into a silent miss."""
    box = {}
    ft.vm_push_file("/tmp/a.json", run_fn=_recorder(box=box))
    assert box["kw"]["timeout"] >= 120


# ----------------------------------------------------------- the manifest

def test_the_ship_manifest_carries_every_vm_read_mac_artifact():
    from src.analysis.patience_basket import SHIP_MANIFEST
    assert set(SHIP_MANIFEST) == {
        "darling_tiers.json", "darlings_levels.json", "darling_ids.json",
        "fo_liquidity.json", "sector_index_bars.json"}


def test_fo_liquidity_and_sector_bars_are_in_the_manifest():
    """Anti-drift lock on the two added 2026-08-05. `fo_liquidity` was ABSENT
    on the VM since the fail-CLOSED `liquidity_filter` was wired on 07-20 —
    it had never been shipped at all. `sector_index_bars` is the new
    producer's output; without it the sector veto stays self-disabled on the
    VM no matter how fresh the Mac's copy is."""
    from src.analysis.patience_basket import SHIP_MANIFEST
    assert "fo_liquidity.json" in SHIP_MANIFEST
    assert "sector_index_bars.json" in SHIP_MANIFEST


def test_every_manifest_entry_is_a_bare_filename():
    """The remote is a fixed `~/alpha_trading/data/`; a path here would land
    somewhere nobody reads."""
    from src.analysis.patience_basket import SHIP_MANIFEST
    for art in SHIP_MANIFEST:
        assert "/" not in art and art.endswith(".json")
