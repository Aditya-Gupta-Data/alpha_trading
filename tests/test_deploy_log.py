"""
Tests for the startup deploy log (src/deploy_log.py). Offline.

Run either of these from the project folder:
    python tests/test_deploy_log.py
    python -m pytest tests/test_deploy_log.py
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import deploy_log

import tempfile


def _fake_git(sha):
    return {"sha": sha, "subject": f"commit {sha}", "committed":
            "2026-07-13T09:00:00+05:30", "dirty": False}


def test_first_start_is_deploy_then_restart_then_new_sha_is_deploy():
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "logs" / "deploy_log.jsonl"
        with patch.object(deploy_log, "_git_state",
                          return_value=_fake_git("aaa1111")):
            first = deploy_log.record_startup("api_server", log_path=log)
            second = deploy_log.record_startup("api_server", log_path=log)
        with patch.object(deploy_log, "_git_state",
                          return_value=_fake_git("bbb2222")):
            third = deploy_log.record_startup("api_server", log_path=log)
        assert first["event"] == "deploy"       # no history yet
        assert second["event"] == "restart"     # same sha came back up
        assert third["event"] == "deploy"       # new code went live
        lines = [json.loads(l) for l in log.read_text().splitlines()]
        assert [e["sha"] for e in lines] == ["aaa1111", "aaa1111", "bbb2222"]


def test_services_tracked_independently():
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "deploy_log.jsonl"
        with patch.object(deploy_log, "_git_state",
                          return_value=_fake_git("aaa1111")):
            deploy_log.record_startup("api_server", log_path=log)
            bot = deploy_log.record_startup("discord_bot", log_path=log)
        # the bot's first start is a deploy even though api_server
        # already logged the same sha — history is per service
        assert bot["event"] == "deploy"


def test_real_git_lookup_and_corrupt_lines_tolerated():
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "deploy_log.jsonl"
        log.write_text("not json at all\n")     # corruption must not raise
        entry = deploy_log.record_startup("scheduler", log_path=log)
        # this repo IS a git repo, so the real lookup should resolve
        assert entry["sha"] not in ("", None)
        assert entry["event"] == "deploy"


def test_fail_open_when_git_missing():
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "deploy_log.jsonl"
        entry = deploy_log.record_startup(
            "api_server", repo_root=Path(tmp), log_path=log)
        assert entry is not None                # never takes a service down
        assert entry["sha"] == "unknown"


if __name__ == "__main__":
    test_first_start_is_deploy_then_restart_then_new_sha_is_deploy()
    test_services_tracked_independently()
    test_real_git_lookup_and_corrupt_lines_tolerated()
    test_fail_open_when_git_missing()
    print("deploy_log: all tests passed")
