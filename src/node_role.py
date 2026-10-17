"""
src/node_role.py — the VM is the only execution node; the home node is the analytics hub (decision #146).

Stdlib-only. A host is the analytics node when ALPHA_NODE_ROLE=analytics (process env or the repo
.env), when a node crontab block's ALPHA_NODE_REPLICA / ALPHA_NODE_SHADOW is set, or when its
hostname or ALPHA_NODE_LABEL is in ANALYTICS_HOSTS. There the live loop refuses to start unless
ALPHA_ALLOW_EXECUTION=1 is in the process env (a hand-run failover, only with the VM's session stopped).
"""
from __future__ import annotations

import os
import socket
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
ANALYTICS_HOSTS = ("minipc1",)

# Never scheduled on the node: the VM owns the one Dhan token and the dashboard mirror (#144) ...
NOT_ON_NODE = ("src.renew_token", "scripts/publish_dashboard_mirror.sh")
# ... and the live session lane: the loop itself and every 09:10–15:50 Dhan consumer (#146).
EXECUTION_JOBS = ("src.master_scheduler", "src.live_bridge", "src.execution.recon_engine",
                  "src.ingestion.intraday_tracker", "src.ingestion.chain_archiver")
NEVER_ON_NODE = NOT_ON_NODE + EXECUTION_JOBS


def never_on_node(job: str) -> bool:
    """True for a cron line or job key the node must not run."""
    return any(x in job for x in NEVER_ON_NODE)


def _dotenv(key: str, env_path: Path) -> str:
    try:
        for line in Path(env_path).read_text().splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip() == key:
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _get(key: str, env, env_path: Path) -> str:
    return (env.get(key) or _dotenv(key, env_path) or "").strip()


def is_analytics_node(env=None, hostname: str | None = None, env_path: Path = ENV_PATH) -> bool:
    env = os.environ if env is None else env
    if _get("ALPHA_NODE_ROLE", env, env_path).lower() == "analytics":
        return True
    if any(_get(k, env, env_path) not in ("", "0") for k in ("ALPHA_NODE_REPLICA", "ALPHA_NODE_SHADOW")):
        return True
    host = (socket.gethostname() if hostname is None else hostname).split(".")[0].lower()
    label = _get("ALPHA_NODE_LABEL", env, env_path).lower()
    return host in ANALYTICS_HOSTS or label in ANALYTICS_HOSTS


def execution_refusal(env=None, hostname: str | None = None, env_path: Path = ENV_PATH) -> str | None:
    """None where the live loop may run; otherwise the reason it must not."""
    env = os.environ if env is None else env
    if not is_analytics_node(env, hostname, env_path):
        return None
    if (env.get("ALPHA_ALLOW_EXECUTION") or "").strip() == "1":      # process env only, never a standing .env line
        return None
    return ("this host is the analytics node; the GCP VM is the only execution node (decision #146). "
            "Set ALPHA_ALLOW_EXECUTION=1 only for a failover with the VM's own session stopped.")
