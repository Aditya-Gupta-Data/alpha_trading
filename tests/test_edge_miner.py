"""
Edge miner tests — fully offline: gcloud is a fake runner, Ollama checks
are patched, the mining step is a stub that writes edges directly, and
all state/data paths point at temp dirs (the real data/ is never touched).

Run from the project folder:
    python tests/test_edge_miner.py      (simple, no extra installs)
    python -m pytest tests/              (if you have pytest)
"""

import json
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src import edge_miner as em
from src.graph_engine import add_edge, ensure_schema


def test_due_gate_never_ran_is_due():
    with tempfile.TemporaryDirectory() as tmp:
        assert em.due(state_path=Path(tmp) / "missing.json") is True


def test_due_gate_blocks_recent_and_allows_old_runs():
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.json"
        now = time.time()
        state.write_text(json.dumps({"last_success": now - 3600}))   # 1h ago
        assert em.due(state_path=state, now=now) is False
        state.write_text(json.dumps({"last_success": now - 21 * 3600}))
        assert em.due(state_path=state, now=now) is True
        state.write_text("corrupted{{{")                             # junk
        assert em.due(state_path=state, now=now) is True


def test_run_skips_without_ollama_or_when_not_due():
    with mock.patch.object(em, "due", return_value=False):
        assert em.run_miner()["status"] == "skipped"
    with mock.patch.object(em, "due", return_value=True), \
         mock.patch.object(em, "ollama_up", return_value=False):
        result = em.run_miner()
    assert result["status"] == "skipped" and "Ollama" in result["reason"]


def _seed_db(path: Path, existing_triples=()):
    conn = brain_map.connect(str(path))
    ensure_schema(conn)
    for s, r, t in existing_triples:
        add_edge(conn, s, r, t, confidence_score=1.0)
    conn.close()


def test_mine_new_triples_reports_only_the_delta():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "brain_map.db"
        _seed_db(db, existing_triples=[("old_a", "RESULTS_IN", "old_b")])

        def fake_task_d(conn, extractor=None, window_days=None, today=None):
            add_edge(conn, "old_a", "RESULTS_IN", "old_b")       # reinforce
            add_edge(conn, "iron_condor", "RESULTS_IN", "win",
                     confidence_score=1.0, context="mined")
            return {"outcomes_considered": 2, "triples_written": 2,
                    "triples_skipped": 0}

        with mock.patch("src.sleep_phase.write_causal_links", fake_task_d):
            stats, new = em.mine_new_triples(db)
    assert stats["triples_written"] == 2
    assert len(new) == 1                       # the reinforce is NOT new
    assert new[0]["source"] == "iron_condor" and new[0]["target"] == "win"
    assert new[0]["confidence"] == 1.0


def _fake_runner_factory(seed_triples, calls):
    """A gcloud stand-in: 'pulls' by writing a seeded DB to the local
    destination, records every command, succeeds at everything."""
    def fake_runner(cmd, timeout=120):
        calls.append(cmd)
        if "scp" in cmd:
            dest = cmd[-1]
            if dest.endswith("brain_map.db"):        # the PULL step
                _seed_db(Path(dest), existing_triples=seed_triples)
        return mock.Mock(returncode=0, stdout="ok", stderr="")
    return fake_runner


def test_full_cycle_pull_mine_apply_refresh():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        data_dir, state = tmp / "data", tmp / "data" / ".state.json"
        data_dir.mkdir()
        (data_dir / "brain_map.db").write_text("old local copy")
        calls = []

        def fake_task_d(conn, extractor=None, window_days=None, today=None):
            add_edge(conn, "vix_spike", "PRECEDES", "condor_loss",
                     confidence_score=1.0)
            return {"outcomes_considered": 5, "triples_written": 1,
                    "triples_skipped": 0}

        with mock.patch.object(em, "due", return_value=True), \
             mock.patch.object(em, "ollama_up", return_value=True), \
             mock.patch.object(em, "extractor_ready", return_value=(True, "ok")), \
             mock.patch.object(em, "_gcloud", return_value="/fake/gcloud"), \
             mock.patch.object(em, "DATA_DIR", data_dir), \
             mock.patch.object(em, "STATE_PATH", state), \
             mock.patch.object(em, "ARCHIVE_DIR", data_dir / "archive"), \
             mock.patch("src.sleep_phase.write_causal_links", fake_task_d):
            result = em.run_miner(
                runner=_fake_runner_factory([("seed", "RESULTS_IN", "x")],
                                            calls))

        assert result["status"] == "ok"
        assert result["new_edges_applied_to_vm"] == 1
        assert state.exists()                       # success recorded
        # the pre-existing local file was archived exactly once
        assert (data_dir / "archive" / "brain_map.db").exists()
        # command sequence: pull scp, ship-edges+applier scp, remote
        # apply ssh, refresh scp
        kinds = ["ssh" if "ssh" in c else "scp" for c in calls]
        assert kinds == ["scp", "scp", "ssh", "scp"]
        ship_cmd = calls[1]
        # BOTH files travel in one scp: the payload AND the applier
        # script (multi-line python via ssh --command gets newline-
        # mangled by the remote shell — the applier must be a file)
        assert any("new_edges.json" in c for c in ship_cmd)
        assert any("apply_edges.py" in c for c in ship_cmd)
        apply_cmd = calls[2][-1]
        assert "/tmp/apply_edges.py" in apply_cmd
        assert "/tmp/new_edges.json" in apply_cmd
        assert "-c" not in calls[2]        # never inline python over ssh


def test_no_new_edges_means_no_apply_call():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        data_dir, state = tmp / "data", tmp / "data" / ".state.json"
        data_dir.mkdir()
        calls = []

        def fake_task_d(conn, extractor=None, window_days=None, today=None):
            return {"outcomes_considered": 0, "triples_written": 0,
                    "triples_skipped": 0}

        with mock.patch.object(em, "due", return_value=True), \
             mock.patch.object(em, "ollama_up", return_value=True), \
             mock.patch.object(em, "extractor_ready", return_value=(True, "ok")), \
             mock.patch.object(em, "_gcloud", return_value="/fake/gcloud"), \
             mock.patch.object(em, "DATA_DIR", data_dir), \
             mock.patch.object(em, "STATE_PATH", state), \
             mock.patch.object(em, "ARCHIVE_DIR", data_dir / "archive"), \
             mock.patch("src.sleep_phase.write_causal_links", fake_task_d):
            result = em.run_miner(runner=_fake_runner_factory([], calls))

        assert result["status"] == "ok"
        assert result["new_edges_applied_to_vm"] == 0
        kinds = ["ssh" if "ssh" in c else "scp" for c in calls]
        assert kinds == ["scp", "scp"]              # pull + refresh only


def test_failed_pull_reports_and_writes_no_state():
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / ".state.json"

        def failing_runner(cmd, timeout=120):
            return mock.Mock(returncode=1, stdout="", stderr="scp: boom")

        with mock.patch.object(em, "due", return_value=True), \
             mock.patch.object(em, "ollama_up", return_value=True), \
             mock.patch.object(em, "extractor_ready", return_value=(True, "ok")), \
             mock.patch.object(em, "_gcloud", return_value="/fake/gcloud"), \
             mock.patch.object(em, "STATE_PATH", state):
            result = em.run_miner(runner=failing_runner)

        assert result["status"] == "failed"
        assert "pull" in result["reason"]
        assert not state.exists()                   # failure never gates


# --- Issue 9 honesty guard: the end-to-end extractor probe ------------------

def test_extractor_probe_passes_on_a_valid_frame():
    good = mock.Mock()
    good.extract_event_json.return_value = {
        "event_type": "market_move", "tag": "bank_earnings",
        "sentiment": 2, "entities": ["NIFTY 50"]}
    ok, reason = em.extractor_ready(good)
    assert ok is True and reason == "ok"
    good.extract_event_json.assert_called_once_with(em.PROBE_TEXT)


def test_extractor_probe_fails_on_none_bad_shape_or_raise():
    for rigged in (None, "not a dict", {}, {"tag": "x"}):   # no event_type
        broken = mock.Mock()
        broken.extract_event_json.return_value = rigged
        ok, reason = em.extractor_ready(broken)
        assert ok is False and "no valid event frame" in reason
    exploding = mock.Mock()
    exploding.extract_event_json.side_effect = RuntimeError("boom")
    ok, reason = em.extractor_ready(exploding)
    assert ok is False and "probe raised" in reason


def test_run_miner_skips_honestly_when_the_extractor_is_dead():
    """The Issue 9 regression: Ollama's server answers (ping passes) but
    the extractor chain is non-functional — the run must SKIP with an
    explicit reason, never report ok, and never mark success."""
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / ".state.json"
        with mock.patch.object(em, "due", return_value=True), \
             mock.patch.object(em, "ollama_up", return_value=True), \
             mock.patch.object(em, "extractor_ready",
                               return_value=(False, "dummy extraction "
                                             "returned no valid event frame")), \
             mock.patch.object(em, "STATE_PATH", state):
            result = em.run_miner()
        assert result["status"] == "skipped"
        assert "extractor unavailable" in result["reason"]
        assert not state.exists()


def test_injected_extractors_are_not_probed():
    """Tests/callers that inject their own extractor own its readiness —
    the probe only guards the scheduled build-your-own path."""
    with mock.patch.object(em, "due", return_value=True), \
         mock.patch.object(em, "ollama_up", return_value=True), \
         mock.patch.object(em, "extractor_ready") as probe, \
         mock.patch.object(em, "_gcloud", return_value=None):
        result = em.run_miner(extractor=mock.Mock())
    assert not probe.called
    assert result["reason"] == "gcloud CLI not found"   # got past the guard


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError:
            print(f"FAIL  {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed.")


# ------------------------------------------------ transport resilience
#
# 2026-08-05. The pull failed three nights running on three faces of one
# problem — a fragile SSH hop from a home connection to us-central1:
#   08-02  client_loop: send disconnect: Broken pipe
#   08-02  subprocess.TimeoutExpired after 120s  (escaped as a TRACEBACK)
#   08-03  kex_exchange_identification: read: Operation timed out
# Each transient event killed the entire nightly cycle: no retry, no
# keep-alive, and one uncaught exception path. The DB is 3.6MB and a healthy
# pull measures ~10s, so this was never bandwidth — it was handshake
# fragility. These pin the fix, with the clock injected so nothing sleeps.

class _P:
    def __init__(self, rc=0, stderr=""):
        self.returncode, self.stderr, self.stdout = rc, stderr, ""


def _sequence(*results):
    """A runner that returns/raises each result in turn, recording calls."""
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        r = results[min(len(calls) - 1, len(results) - 1)]
        if isinstance(r, Exception):
            raise r
        return r
    return runner, calls


def test_broken_pipe_is_retried_and_then_succeeds():
    runner, calls = _sequence(
        _P(255, "client_loop: send disconnect: Broken pipe"), _P(0))
    slept = []
    res = em.run_resilient(runner, ["gcloud"], "pull", sleep_fn=slept.append)
    assert res.returncode == 0
    assert len(calls) == 2
    assert slept == [em.TRANSPORT_BACKOFF]


def test_the_kex_handshake_timeout_is_retried():
    runner, calls = _sequence(
        _P(255, "kex_exchange_identification: read: Operation timed out"),
        _P(255, "banner exchange: Connection to 35.239.254.99 port 22: "
                "Operation timed out"),
        _P(0))
    res = em.run_resilient(runner, ["gcloud"], "pull", sleep_fn=lambda s: None)
    assert res.returncode == 0 and len(calls) == 3


def test_a_timeout_expired_no_longer_escapes_as_a_traceback():
    """The 08-02 failure mode: subprocess.TimeoutExpired propagated straight
    out of run_miner. It must become an ordinary failed result."""
    import subprocess as sp
    runner, calls = _sequence(sp.TimeoutExpired(["gcloud"], 180), _P(0))
    res = em.run_resilient(runner, ["gcloud"], "pull", sleep_fn=lambda s: None)
    assert res.returncode == 0 and len(calls) == 2


def test_every_attempt_failing_returns_the_last_result_not_an_exception():
    import subprocess as sp
    runner, calls = _sequence(sp.TimeoutExpired(["gcloud"], 180))
    res = em.run_resilient(runner, ["gcloud"], "pull", sleep_fn=lambda s: None)
    assert res.returncode == 124
    assert len(calls) == em.TRANSPORT_ATTEMPTS
    assert "TimeoutExpired" in res.stderr


def test_backoff_is_exponential_not_flat():
    runner, _ = _sequence(_P(255, "Broken pipe"))
    slept = []
    em.run_resilient(runner, ["gcloud"], "pull", attempts=4, backoff=5,
                     sleep_fn=slept.append)
    assert slept == [5, 10, 20]


def test_a_non_transient_failure_is_NOT_retried():
    """Retrying a missing file or an auth refusal three times just costs
    three minutes and hides the real reason."""
    runner, calls = _sequence(_P(1, "ERROR: (gcloud.compute.scp) [Errno 2] "
                                    "No such file or directory"))
    res = em.run_resilient(runner, ["gcloud"], "pull", sleep_fn=lambda s: None)
    assert res.returncode == 1
    assert len(calls) == 1


def test_the_transient_classifier_knows_the_three_real_failures():
    for real in ("client_loop: send disconnect: Broken pipe",
                 "kex_exchange_identification: read: Operation timed out",
                 "banner exchange: Connection to 35.239.254.99 port 22: "
                 "Operation timed out",
                 "/usr/bin/scp: Connection closed",
                 "exited with return code [255]"):
        assert em._transient(real) is True
    assert em._transient("No such file or directory") is False
    assert em._transient("PERMISSION_DENIED") is False
    assert em._transient("") is False


def test_keepalive_flags_ride_on_both_transports_with_the_right_flag_name():
    """`gcloud compute scp` does NOT accept the `-- -o ...` passthrough that
    `gcloud compute ssh` does — it reads the flags as extra source paths
    (verified live). The two flag names are not interchangeable."""
    assert "--scp-flag=ServerAliveInterval=60" in em.SCP_FLAGS
    assert "--ssh-flag=ServerAliveInterval=60" in em.SSH_FLAGS
    assert "--scp-flag=ServerAliveCountMax=3" in em.SCP_FLAGS
    assert "--scp-flag=ConnectTimeout=30" in em.SCP_FLAGS
    assert all(f.startswith("--scp-flag=") for f in em.SCP_FLAGS)
    assert all(f.startswith("--ssh-flag=") for f in em.SSH_FLAGS)


def test_the_per_attempt_timeout_was_raised_above_the_one_that_expired():
    assert em.TRANSPORT_TIMEOUT > 120


def test_gcloud_runs_with_a_pinned_interpreter():
    """The standing rule from the 08-05 ship fix — an unattended job must
    not inherit whatever python happens to be on PATH."""
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return _P(0)
    with mock.patch("subprocess.run", side_effect=fake_run):
        em._run(["gcloud", "version"])
    assert seen["env"]["CLOUDSDK_PYTHON"] == sys.executable
    assert seen["timeout"] == em.TRANSPORT_TIMEOUT
