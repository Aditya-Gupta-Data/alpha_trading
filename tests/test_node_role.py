"""Decision #146: the VM is the only execution node; the home node never starts the live loop."""
import asyncio
import runpy
import sys
from pathlib import Path

import pytest

from src import deploy_log, node_role

ROOT = Path(__file__).resolve().parent.parent
VM = "alpha-trading-vm"


def test_the_vm_and_the_mac_may_run_the_loop(tmp_path):
    for host in (VM, "Adityas-MacBook-Air"):
        assert node_role.execution_refusal({}, host, tmp_path / "none") is None


@pytest.mark.parametrize("env,host,dotenv", [
    ({"ALPHA_NODE_ROLE": "analytics"}, VM, ""),
    ({}, VM, "ALPHA_NODE_ROLE=analytics\n"),
    ({"ALPHA_NODE_REPLICA": "1"}, VM, ""),
    ({"ALPHA_NODE_SHADOW": "1"}, VM, ""),
    ({}, "minipc1", ""),
    ({}, "minipc1.tailnet.ts.net", ""),
    ({"ALPHA_NODE_LABEL": "minipc1"}, VM, ""),
    ({}, VM, 'ALPHA_NODE_LABEL="minipc1"\n'),
])
def test_every_node_marker_refuses_the_loop(tmp_path, env, host, dotenv):
    p = tmp_path / ".env"
    p.write_text(dotenv)
    assert node_role.is_analytics_node(env, host, p)
    assert "decision #146" in node_role.execution_refusal(env, host, p)


def test_a_promoted_home_lane_block_still_counts_on_the_hostname(tmp_path):
    assert not node_role.is_analytics_node({"ALPHA_NODE_SHADOW": "0"}, VM, tmp_path / "none")
    assert node_role.is_analytics_node({"ALPHA_NODE_SHADOW": "0"}, "minipc1", tmp_path / "none")


def test_the_failover_override_is_process_env_only(tmp_path):
    p = tmp_path / ".env"
    p.write_text("ALPHA_NODE_ROLE=analytics\nALPHA_ALLOW_EXECUTION=1\n")
    assert node_role.execution_refusal({}, "minipc1", p) is not None          # a standing .env line never lifts it
    assert node_role.execution_refusal({"ALPHA_ALLOW_EXECUTION": "1"}, "minipc1", p) is None


def test_the_job_lists():
    for job in ("src.master_scheduler", "src.execution.recon_engine", "src.ingestion.intraday_tracker --darlings",
                "src.ingestion.chain_archiver", "src.renew_token", "scripts/publish_dashboard_mirror.sh"):
        assert node_role.never_on_node(job), job
    for job in ("src.validation.run_proving_court", "src.analysis.macro_nightly", "src.discovery.nightly",
                "src.ingestion.bhavcopy_clerk --backfill 5", "src.main", "src.firm_treasury --rotate"):
        assert not node_role.never_on_node(job), job


@pytest.mark.parametrize("module", ["src.master_scheduler", "src.live_bridge"])
def test_the_entry_points_refuse_on_the_node_before_anything_runs(monkeypatch, module):
    ran = []
    def boom(*a, **k):
        ran.append(a)
        if a and asyncio.iscoroutine(a[0]):
            a[0].close()
        raise AssertionError("the loop started on the analytics node")
    monkeypatch.setattr(asyncio, "run", boom)
    monkeypatch.setattr(deploy_log, "record_startup", boom)
    monkeypatch.setenv("ALPHA_NODE_ROLE", "analytics")
    monkeypatch.delenv("ALPHA_ALLOW_EXECUTION", raising=False)
    monkeypatch.delitem(sys.modules, module, raising=False)
    with pytest.raises(SystemExit) as e:
        runpy.run_module(module, run_name="__main__")
    assert e.value.code == 3 and ran == []


def test_the_vm_installer_refuses_on_the_node_before_it_touches_cron():
    text = (ROOT / "scripts" / "setup_cron.sh").read_text()
    assert text.index("REFUSED: this host is the analytics node") < text.index("| crontab -")
    assert "ALPHA_NODE_ROLE=analytics" in text and '"minipc1"' in text


def test_every_node_installer_declares_the_role():
    for name in ("setup_node_replica_cron.sh", "setup_mininode_cron.sh", "bootstrap_node.sh", "node_preflight.sh"):
        assert "ALPHA_NODE_ROLE=analytics" in (ROOT / "scripts" / name).read_text(), name
