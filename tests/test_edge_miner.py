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
        # command sequence: pull scp, ship-edges scp, remote apply ssh,
        # refresh scp
        kinds = ["ssh" if "ssh" in c else "scp" for c in calls]
        assert kinds == ["scp", "scp", "ssh", "scp"]
        apply_cmd = calls[2][-1]
        assert "add_edge" in apply_cmd and "new_edges.json" in apply_cmd


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
             mock.patch.object(em, "_gcloud", return_value="/fake/gcloud"), \
             mock.patch.object(em, "STATE_PATH", state):
            result = em.run_miner(runner=failing_runner)

        assert result["status"] == "failed"
        assert "pull" in result["reason"]
        assert not state.exists()                   # failure never gates


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
