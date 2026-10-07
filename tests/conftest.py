"""
tests/conftest.py — the suite's hermeticity floor (RULE 6).

Created 2026-08-19 after `test_pm_run_headless_silently_rejects_when_the_
gate_says_no` failed exactly once on the VM mid-session and then passed
five times running. The cause was not the test: it was reading TWO pieces
of live production state that only exist on the trading box —

  1. `PAPER_AUTO_APPROVE=1` from the VM's real environment/.env, which
     flipped `run_headless` onto its auto-approve branch, and
  2. the real `data/human_pulse.json`, whose engagement tripwire was in a
     tripped-but-not-yet-alerted episode, so the branch fired an extra
     🛑 unsupervised card through the stubbed notifier and the "exactly
     one Discord message" assertion saw two.

Worse than the red: `should_alert_once()` stamps `alerted_at`, so the
failing run CONSUMED the owner's one-per-episode card and the next run
was green. A self-erasing failure that also suppressed a real alert.

These fixtures are autouse and deliberately blunt. A test that wants
either switch must set it itself (`mock.patch.dict(os.environ, ...)` or
an explicit `path=`), which is how the intent becomes visible in the test
instead of inherited from whatever box the suite happens to run on.
"""
import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# THE PRODUCTION WRITE GUARD (2026-09-30, ledger Issue 37).
#
# An audit found 18 tests in 8 files WRITING into this checkout's real
# `data/` and `logs/`. On the owner's Mac that is working data: every
# `deals_census` partition in the lake (37 of them, 07-11 -> 09-30) and every
# row of `data/rss_signals.jsonl` (1,013) turned out to be test fixtures.
#
# A process-wide audit hook (PEP 578) now refuses, for the whole session,
# any of these aimed at a path under `<repo>/data` or `<repo>/logs`:
#   * open() / os.open() in a write mode (w, a, x, +, or O_WRONLY / O_RDWR /
#     O_CREAT / O_APPEND / O_TRUNC) — covers write_text, json dumps,
#     tempfile.mkstemp, atomic .tmp + os.replace, lock files;
#   * os.remove / os.unlink / os.rename / os.replace / os.rmdir /
#     shutil.rmtree / os.truncate;
#   * sqlite3.connect (a default connect creates and writes the file).
# The hook RAISES `ProdWriteBlocked` before the operation runs, so nothing
# reaches the disk. Because fail-open production code may swallow that
# exception, every refusal is also recorded, and the autouse fixture below
# FAILS the test that caused it, naming the path. A test that needs to write
# must inject a tmp_path (or monkeypatch the module's path constant).
# Reads are not blocked (a separate, wider problem — see Issue 37).
# ---------------------------------------------------------------------------
_REPO = Path(__file__).resolve().parent.parent
_PROTECTED = tuple(os.path.realpath(_REPO / d) for d in ("data", "logs"))
_WRITE_FLAGS = (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND
                | os.O_TRUNC)
# event -> ((path arg index, dir_fd arg index | None), ...). A RELATIVE path
# with a dir_fd is anchored to that directory, not to the cwd (shutil.rmtree
# removes a temp dir's `logs/` child that way), so it is not resolved here;
# a real rmtree of data/ is still caught by its own "shutil.rmtree" event.
_PATH_EVENTS = {"os.remove": ((0, 1),), "os.rmdir": ((0, 1),),
                "os.rename": ((0, 2), (1, 3)), "shutil.rmtree": ((0, 1),),
                "os.truncate": ((0, None),), "sqlite3.connect": ((0, None),)}
_blocked = []            # (event, path) refused since the last reset


class ProdWriteBlocked(PermissionError):
    """A test tried to write into the real data/ or logs/ (Issue 37)."""


def _protected_path(target):
    if isinstance(target, int):
        return None                          # an fd: its open was audited
    try:
        raw = os.fsdecode(target)
    except TypeError:
        return None
    if raw in ("", ":memory:") or (raw.startswith("file:")
                                   and "mode=ro" in raw):
        return None
    if raw.startswith("file:"):
        raw = raw[5:].split("?", 1)[0]
    real = os.path.realpath(os.path.abspath(raw))
    for root in _PROTECTED:
        if real == root or real.startswith(root + os.sep):
            return real
    return None


def _prod_write_audit(event, args):
    if event == "open":
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        writes = (any(c in mode for c in "wax+") if isinstance(mode, str)
                  else bool(isinstance(flags, int) and flags & _WRITE_FLAGS))
        if not writes:
            return
        hits = [_protected_path(args[0])]
    elif event in _PATH_EVENTS:
        hits = []
        for i, fd_i in _PATH_EVENTS[event]:
            if i >= len(args):
                continue
            anchored = (fd_i is not None and fd_i < len(args)
                        and args[fd_i] is not None)
            if anchored and not os.path.isabs(os.fsdecode(args[i])):
                continue
            hits.append(_protected_path(args[i]))
    else:
        return
    for real in hits:
        if real:
            _blocked.append((event, real))
            raise ProdWriteBlocked(
                f"RULE 6 write guard: a test tried {event} on {real} — "
                "inject a tmp_path instead (ledger Issue 37)")


sys.addaudithook(_prod_write_audit)


@pytest.fixture(autouse=True)
def _no_writes_into_real_data_or_logs():
    """Fail the test that caused any refused write, even if the code under
    test swallowed the ProdWriteBlocked it raised."""
    _blocked.clear()
    yield
    if _blocked:
        seen = sorted({f"{ev} {os.path.relpath(p, _REPO)}"
                       for ev, p in _blocked})
        _blocked.clear()
        pytest.fail("test wrote (or tried to write) into the real data/ or "
                    "logs/: " + "; ".join(seen), pytrace=False)

# Ambient switches that change ENGINE BEHAVIOUR and exist on the VM.
# Cleared for every test; a test that needs one sets it explicitly.
_AMBIENT_ENGINE_SWITCHES = ("PAPER_AUTO_APPROVE",)


@pytest.fixture(autouse=True)
def _no_ambient_engine_switches(monkeypatch):
    """The suite must behave identically on the Mac and on the VM."""
    for key in _AMBIENT_ENGINE_SWITCHES:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _isolated_human_pulse(monkeypatch, tmp_path):
    """Point the engagement tripwire at a per-test temp file. `touch()`
    and `should_alert_once()` are muzzled under pytest on the default
    path, but `auto_approve_tripped()` READS it — and a read of live
    supervision state is what made the VM failure order-dependent."""
    from src import human_pulse
    monkeypatch.setattr(human_pulse, "PULSE_PATH",
                        tmp_path / "human_pulse.json")


@pytest.fixture(autouse=True)
def _isolated_brain_map(monkeypatch, tmp_path):
    """`brain_map.connect()` with no path opens (and creates, and writes)
    the real data/brain_map.db. 77 tests in 26 files reached it that way
    (Issue 37, found by the write guard above); on the Mac that is the
    owner's database. Every test now gets its own empty one. A test that
    wants a specific database passes its own path or ':memory:'."""
    from src import brain_map, eod_summary
    monkeypatch.setattr(brain_map, "DEFAULT_DB_PATH",
                        tmp_path / "brain_map.db")
    # eod_summary keeps its own copy of the path (read by the CEO brief)
    monkeypatch.setattr(eod_summary, "DEFAULT_DB_PATH",
                        tmp_path / "brain_map.db")


@pytest.fixture(autouse=True)
def _isolated_dashboard_reads(monkeypatch, tmp_path):
    """The showcase dashboard's readers default to the REAL data/ + logs/
    files (read-only, so the write guard never sees them). Since audit F10
    (2026-10-06) `open_trades()` also reads the live arm's book from its
    DB_PATH, so a test that injects only a journal read the owner's
    brain_map.db on the Mac — and would read the VM's on the VM. Every
    default points at this test's tmp dir; the database is the same file
    `brain_map.connect()` opens here, so a test's own book is what the
    page reads. A test that wants other files passes them."""
    from src.dashboard import data as dashboard_data
    monkeypatch.setattr(dashboard_data, "DB_PATH", tmp_path / "brain_map.db")
    monkeypatch.setattr(dashboard_data, "JOURNAL_PATH", tmp_path / "journal.jsonl")
    monkeypatch.setattr(dashboard_data, "EQUITY_LEDGER_PATH", tmp_path / "equity_shadow_journal.jsonl")
    monkeypatch.setattr(dashboard_data, "SNAPSHOT_PATH", tmp_path / "market_snapshot.json")
    monkeypatch.setattr(dashboard_data, "BENCHMARKS_PATH", tmp_path / "dashboard_benchmarks.json")
    monkeypatch.setattr(dashboard_data, "RECON_PATH", tmp_path / "recon.jsonl")


@pytest.fixture(autouse=True)
def _isolated_shared_runtime_files(monkeypatch, tmp_path):
    """Host-wide state files any test can reach through a real code path
    (Issue 37): the Discord daily budget (an unmuzzled dispatch in a test
    used to spend the owner's budget), the cross-process Dhan throttle slots
    (the general gate and the chain lane),
    and the adaptive-sizing ledger — its `record()` READS the last action
    per key as de-dup memory and appends when it differs, so on the Mac 23
    tests in 7 files appended to the real logs/sizing_adjustments.jsonl and
    their path depended on what the owner's ledger held. Also the Discord
    digest queue: `eod_summary.build_eod_card()` drains the default
    logs/discord_digest_queue.jsonl, so while the owner's queue held cards
    (the Mac 19:15 chain spools them every weekday) about a dozen tests
    either drained real cards into `.drained` unsent (before the guard) or
    failed on the guard (after it). Each test gets its own. So does the
    live arm's single-instance tick lock (audit F06): every `tick` takes
    it on its real path, with no pytest branch to hide that path."""
    from src import adaptive_sizing, dhan_client, journal, notifier
    from src.execution import live_pricer
    # The journal and its write lock (decision #122: every journal write
    # takes journal.lock beside the file) — a test that fakes read_all /
    # rewrite_all still takes the lock, so the lock must live in tmp too.
    monkeypatch.setattr(journal, "DATA_DIR", tmp_path)
    monkeypatch.setattr(journal, "JOURNAL_PATH", tmp_path / "journal.jsonl")
    monkeypatch.setattr(adaptive_sizing, "ADJUSTMENTS_PATH",
                        tmp_path / "sizing_adjustments.jsonl")
    monkeypatch.setattr(notifier, "BUDGET_STATE_PATH",
                        tmp_path / ".discord_budget.json")
    monkeypatch.setattr(notifier, "DIGEST_QUEUE_PATH",
                        tmp_path / "discord_digest_queue.jsonl")
    monkeypatch.setattr(dhan_client, "_THROTTLE_FILE",
                        tmp_path / ".dhan_throttle")
    monkeypatch.setattr(dhan_client, "_CHAIN_THROTTLE_FILE",
                        tmp_path / ".dhan_chain_throttle")
    monkeypatch.setattr(live_pricer, "TICK_LOCK_FILE",
                        tmp_path / ".live_pricer_tick.lock")
