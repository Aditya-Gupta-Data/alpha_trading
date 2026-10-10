#!/usr/bin/env python3
# MANUAL OFFLINE TOOL — called by scripts/setup_node_replica_cron.sh; nothing schedules it.
"""
scripts/node_replica_block.py — the VM's cron schedule, rendered for the home node.

The replica must run EXACTLY the VM's jobs, so the lines are not retyped: this
reads the heredoc in scripts/setup_cron.sh (the VM's installer and the ground
truth for its schedule), substitutes the node's repo path and interpreter, and
drops the few jobs a replica must not run:

  src.renew_token                       the VM owns the one Dhan token (#48)
  scripts/publish_dashboard_mirror.sh   would overwrite the VM's mirror on the dashboard box

Everything else — including the VM's own comments' absence — is the VM's schedule
verbatim, so it can never drift from it.

    python3 scripts/node_replica_block.py --repo /home/mini_pc1/alpha_trading [--python PY] [--list-excluded]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETUP = ROOT / "scripts" / "setup_cron.sh"
EXCLUDE = ("src.renew_token", "publish_dashboard_mirror.sh")
_CRON = re.compile(r"^[0-9*/,\-]+\s+[0-9*/,\-]+\s+[0-9*/,\-]+\s+[0-9*/,\-]+\s+[0-9*/,\-]+\s+\S")


def heredoc(text: str) -> str:
    """The body of CRON_ENTRIES=$(cat <<EOF … EOF)."""
    m = re.search(r"CRON_ENTRIES=\$\(cat <<EOF\n(.*?)\nEOF\n", text, re.S)
    if not m:
        raise SystemExit("node_replica_block: could not find the CRON_ENTRIES heredoc in setup_cron.sh")
    return m.group(1)


def render(setup_text: str, repo: str, python: str) -> tuple[list[str], list[str]]:
    """(kept lines, excluded lines) with $REPO_ROOT / $PYTHON_BIN substituted."""
    kept, dropped = [], []
    for line in heredoc(setup_text).splitlines():
        if not _CRON.match(line.strip()) or line.lstrip().startswith("#"):
            continue
        line = line.replace("$REPO_ROOT", repo).replace("${REPO_ROOT}", repo) \
                   .replace("$PYTHON_BIN", python).replace("${PYTHON_BIN}", python)
        (dropped if any(x in line for x in EXCLUDE) else kept).append(line)
    return kept, dropped


def ops_expected_jobs(drop=("renew_token.log",)) -> str:
    """The value of OPS_EXPECTED_JOBS for the node: the VM's own heartbeat list
    (src.ops_monitor.EXPECTED_JOBS) minus the jobs a replica does not run, so the
    node's 20:30 sweep and CEO brief never cry 'renew_token.log SILENT' about a job
    that is absent by design. Derived, not retyped: it follows the VM's list."""
    sys.path.insert(0, str(ROOT))
    from src.ops_monitor import EXPECTED_JOBS
    return ",".join(f"{name}:{'1' if weekdays else '0'}" for name, weekdays in EXPECTED_JOBS.items()
                    if name not in drop)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--python", default=None)
    ap.add_argument("--list-excluded", action="store_true")
    ap.add_argument("--ops-env", action="store_true", help="print the OPS_EXPECTED_JOBS value instead of cron lines")
    ap.add_argument("--setup", default=str(SETUP))
    a = ap.parse_args(argv)
    if a.ops_env:
        print(ops_expected_jobs())
        return 0
    kept, dropped = render(Path(a.setup).read_text(), a.repo, a.python or f"{a.repo}/venv/bin/python")
    for line in (dropped if a.list_excluded else kept):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
