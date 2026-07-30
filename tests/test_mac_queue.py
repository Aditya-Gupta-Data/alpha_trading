"""The Mac Handover Queue (src/mac_queue.py) + its macro_nightly producer.

Edge-to-Cloud architecture (owner directive 2026-07-30): the VM declines
heavy work, posts it here, and keeps ticking. Two properties matter more
than the CRUD and are asserted directly: the queue NEVER raises into the
pipeline it protects, and the nightly run does not depend on it.

Hermetic: tmp_path files only. The real queue is pytest-muzzled.
"""

from datetime import date

import pytest

from src import mac_queue


def _q(tmp_path):
    return tmp_path / "mac_pending_tasks.jsonl"


def test_enqueue_then_pending_roundtrip(tmp_path):
    q = _q(tmp_path)
    out = mac_queue.enqueue(mac_queue.REBUILD_MACRO_ARTIFACTS,
                            reason="cache miss on ['shock']",
                            detail="rebuild on the Mac", path=q)
    assert out["created"] is True
    rows = mac_queue.pending(q)
    assert len(rows) == 1
    assert rows[0]["task"] == mac_queue.REBUILD_MACRO_ARTIFACTS
    assert rows[0]["status"] == "pending"


def test_same_task_same_day_does_not_flood(tmp_path):
    """A nightly cron re-detecting the same condition must leave ONE row
    per day, not one per run."""
    q = _q(tmp_path)
    kw = dict(reason="cache miss", path=q, today=date(2026, 7, 30))
    assert mac_queue.enqueue(mac_queue.REBUILD_MACRO_ARTIFACTS, **kw)["created"]
    assert not mac_queue.enqueue(mac_queue.REBUILD_MACRO_ARTIFACTS, **kw)["created"]
    assert not mac_queue.enqueue(mac_queue.REBUILD_MACRO_ARTIFACTS, **kw)["created"]
    assert len(mac_queue.pending(q)) == 1


def test_a_new_day_raises_the_task_again(tmp_path):
    q = _q(tmp_path)
    mac_queue.enqueue(mac_queue.REBUILD_MACRO_ARTIFACTS, reason="miss",
                      path=q, today=date(2026, 7, 30))
    mac_queue.enqueue(mac_queue.REBUILD_MACRO_ARTIFACTS, reason="miss",
                      path=q, today=date(2026, 7, 31))
    # Still ONE open task (latest state wins), but both days are on record.
    assert len(mac_queue.pending(q)) == 1
    assert len(mac_queue._rows(q)) == 2


def test_resolve_closes_it_by_appending_not_rewriting(tmp_path):
    q = _q(tmp_path)
    mac_queue.enqueue(mac_queue.REBUILD_MACRO_ARTIFACTS, reason="miss", path=q)
    before = len(mac_queue._rows(q))

    mac_queue.resolve(mac_queue.REBUILD_MACRO_ARTIFACTS, path=q)

    assert mac_queue.pending(q) == []
    assert len(mac_queue._rows(q)) == before + 1     # history kept
    assert mac_queue._rows(q)[0]["status"] == "pending"   # original intact


def test_describe_is_the_literal_copy_paste_line(tmp_path):
    q = _q(tmp_path)
    mac_queue.enqueue(mac_queue.REBUILD_MACRO_ARTIFACTS, reason="miss", path=q)
    line = mac_queue.describe(path=q)
    assert line.startswith("Claude, please execute the following from the VM queue:")
    assert "rebuild the macro" in line
    assert mac_queue.describe(path=tmp_path / "empty.jsonl") == ""


def test_queue_never_raises_on_a_broken_file(tmp_path):
    """Bookkeeping must never break the pipeline it protects."""
    q = _q(tmp_path)
    q.write_text("not json at all\n{broken\n")
    assert mac_queue.pending(q) == []
    assert mac_queue.enqueue("x", reason="y", path=q)["created"] is True


def test_enqueue_into_an_unwritable_path_fails_open(tmp_path):
    bad = tmp_path / "nope" / "\0" / "q.jsonl"      # invalid path
    assert mac_queue.enqueue("x", reason="y", path=bad)["created"] is False


def test_the_real_queue_is_muzzled_under_pytest():
    """A forgotten fixture must not post fake work to the live queue and
    send the owner chasing a task that never existed."""
    out = mac_queue.enqueue(mac_queue.REBUILD_MACRO_ARTIFACTS, reason="oops")
    assert out == {"created": False, "muzzled": True}
    assert mac_queue.resolve(mac_queue.REBUILD_MACRO_ARTIFACTS) == {
        "created": False, "muzzled": True}


# ------------------------------------------------ the producer wiring

def test_cache_miss_enqueues_and_the_run_still_completes(tmp_path, monkeypatch):
    """The whole architecture in one test: the VM detects heavy work is
    needed, DECLINES it, posts to the queue, and the nightly run finishes."""
    from src.analysis import macro_nightly

    q = _q(tmp_path)
    seen = []
    monkeypatch.setattr(mac_queue, "enqueue",
                        lambda task, reason, detail=None, **kw:
                        seen.append((task, reason)) or {"created": True})

    def fake_declare():
        return {"declared": False,
                "horizons": {"shock": {"declared": False, "phase": None,
                                       "cache_status": "miss_stale",
                                       "best": {}}}}

    res = macro_nightly.run(declare_fn=fake_declare,
                            fred_fn=lambda: {"ok": [], "failed": []},
                            indices_fn=lambda: {"rows_added": {}},
                            scorer_fn=lambda: {"graded": 0},
                            heartbeat_path=tmp_path / "hb.log")

    assert seen and seen[0][0] == mac_queue.REBUILD_MACRO_ARTIFACTS
    assert "ALERT" in res["stages"]["declare"]
    assert "score" in res["stages"]          # the run went on to finish


def test_a_queue_failure_cannot_break_the_nightly_run(tmp_path, monkeypatch):
    """The VM's pipeline must never depend on the queue succeeding."""
    from src.analysis import macro_nightly

    def exploding(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(mac_queue, "enqueue", exploding)

    def fake_declare():
        return {"declared": False,
                "horizons": {"shock": {"declared": False, "phase": None,
                                       "cache_status": "miss", "best": {}}}}

    res = macro_nightly.run(declare_fn=fake_declare,
                            fred_fn=lambda: {"ok": [], "failed": []},
                            indices_fn=lambda: {"rows_added": {}},
                            scorer_fn=lambda: {"graded": 0},
                            heartbeat_path=tmp_path / "hb.log")

    assert "ALERT" in res["stages"]["declare"]
    assert "score" in res["stages"]          # finished despite the explosion
