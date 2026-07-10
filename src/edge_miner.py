"""
Alpha Trading — the opportunistic edge miner (Mac-side)
========================================================

The VM is the trading engine (decision #47), but it has 1GB of RAM and
cannot run Ollama — and the user's rule is NO paid-API tokens for the
knowledge graph's causal mining. So the mining runs HERE, on the Mac,
opportunistically: whenever this machine happens to be awake (login
LaunchAgent + a daily timer), it

  1. GUARDS   — silently skips unless local Ollama answers, gcloud is
                available, and >20h have passed since the last success
                (opportunistic, but roughly daily at most);
  2. PULLS    — the VM's live brain_map.db to a temp copy
                (gcloud compute scp; the VM stays authoritative);
  3. MINES    — runs the Sleep Phase's own Task D
                (sleep_phase.write_causal_links, decision #34: reviewed
                outcomes only) against the temp copy with the local
                Ollama extractor, then diffs graph_edges to collect the
                NEWLY-minted triples;
  4. APPLIES  — ships just those triples (a small JSON) to the VM and
                replays them there through the idempotent
                graph_engine.add_edge — NEVER a whole-file overwrite
                (the VM writes outcomes/post-mortems concurrently;
                clobbering its DB is forbidden);
  5. REFRESHES — pulls fresh read-only copies of brain_map.db and
                journal.jsonl into the Mac's data/ so chat_agent
                answers from near-current state. (The Mac's original
                pre-migration files are archived once, first, into
                data/mac-archive-pre-vm/.)

If the Mac stays closed for a week: nothing breaks. The VM's nightly
sleep phase still decays edge weights; the graph simply learns nothing
NEW until the next time this machine is awake.

Run manually any time:      python3 -m src.edge_miner
Force despite the 20h gate: python3 -m src.edge_miner --force
"""

import argparse
import json
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATE_PATH = DATA_DIR / ".edge_miner_state.json"
ARCHIVE_DIR = DATA_DIR / "mac-archive-pre-vm"

MIN_HOURS_BETWEEN_RUNS = 20
OLLAMA_URL = "http://localhost:11434/api/version"
MINE_WINDOW_DAYS = 30            # how far back Task D looks for outcomes

GCLOUD_CANDIDATES = ("/opt/homebrew/share/google-cloud-sdk/bin/gcloud",
                     "gcloud")
GCP_PROJECT = "project-37632031-10d0-47dd-b6f"
GCP_ZONE = "us-central1-a"
VM = "adigupta1998@alpha-trading-vm"
VM_REPO = "~/alpha_trading"

# The remote applier: replays mined triples through the VM's own
# idempotent writer. Runs with cwd=~/alpha_trading under venv python.
_REMOTE_APPLY = r"""
import json, os, sys
sys.path.insert(0, os.getcwd())   # run by path from /tmp; repo is the cwd
from src import brain_map
from src.graph_engine import add_edge
triples = json.load(open(sys.argv[1]))
conn = brain_map.connect()
for t in triples:
    add_edge(conn, t["source"], t["relation"], t["target"],
             confidence_score=t.get("confidence"),
             context=t.get("context"),
             decay_lambda=t.get("decay_lambda"))
conn.close()
print(f"applied {len(triples)} edge(s)")
"""


def _gcloud() -> str | None:
    for candidate in GCLOUD_CANDIDATES:
        path = shutil.which(candidate) or (
            candidate if Path(candidate).exists() else None)
        if path:
            return path
    return None


def ollama_up(url: str = OLLAMA_URL) -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=3) as resp:
            return resp.status < 500
    except Exception:
        return False


# The honesty probe's input: small, unambiguous, and shaped like the real
# journal text the extractor mines — a valid answer proves the WHOLE chain
# (httpx installed -> Ollama chat endpoint -> model -> JSON coercion), not
# just that a server socket answered.
PROBE_TEXT = ("Health probe: NIFTY 50 closed 1.2 percent higher after "
              "strong bank earnings; volatility eased.")


def extractor_ready(extractor) -> tuple:
    """(ok, reason) from one fast END-TO-END dummy extraction.

    Ledger Issue 9 (2026-07-09): the miner reported "status": "ok" while
    extracting NOTHING — its guard pinged the Ollama *server* (stdlib
    urllib, passed) but the extractor itself needed httpx (absent), so
    every real call silently returned None. A scheduled job's "ok" is only
    trustworthy if the job verifies its own dependencies end-to-end; this
    probe runs the exact code path mining uses and demands a valid event
    frame back. Never raises."""
    try:
        frame = extractor.extract_event_json(PROBE_TEXT)
    except Exception as e:                # extract_event_json shouldn't
        return False, f"probe raised: {e}"  # raise, but never trust that
    if not isinstance(frame, dict) or not frame.get("event_type"):
        return False, ("dummy extraction returned no valid event frame — "
                       "httpx missing, model unloaded, or unusable output")
    return True, "ok"


def due(state_path: Path = None, now: float = None,
        min_hours: float = MIN_HOURS_BETWEEN_RUNS) -> bool:
    """True when the last SUCCESSFUL run is old enough (or never ran)."""
    state_path = state_path if state_path is not None else STATE_PATH
    now = now if now is not None else time.time()
    try:
        last = float(json.loads(state_path.read_text())["last_success"])
    except Exception:
        return True
    return (now - last) >= min_hours * 3600


def _mark_success(state_path: Path = None, now: float = None) -> None:
    state_path = state_path if state_path is not None else STATE_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(
        {"last_success": now if now is not None else time.time(),
         "at": datetime.now().isoformat(timespec="seconds")}))


def snapshot_triples(conn) -> set:
    try:
        return {(r[0], r[1], r[2]) for r in conn.execute(
            "SELECT source_node, relation, target_node FROM graph_edges")}
    except sqlite3.OperationalError:
        return set()  # table not created yet — everything mined is new


def mine_new_triples(db_path: Path, extractor=None,
                     window_days: int = MINE_WINDOW_DAYS) -> tuple:
    """Run Task D against the pulled DB copy; return (stats, new_triples)
    where new_triples is a list of {source, relation, target, confidence}
    for edges that did not exist before this run."""
    from src import brain_map
    from src.sleep_phase import write_causal_links
    conn = brain_map.connect(str(db_path))
    try:
        before = snapshot_triples(conn)
        stats = write_causal_links(conn, extractor=extractor,
                                   window_days=window_days)
        new = []
        for r in conn.execute(
                "SELECT source_node, relation, target_node, "
                "confidence_score, context, decay_lambda FROM graph_edges"):
            if (r[0], r[1], r[2]) not in before:
                # decay_lambda rides along so a loss-derived DECAY-EXEMPT
                # edge (lambda 0, loss-permanence) stays exempt on the VM.
                new.append({"source": r[0], "relation": r[1],
                            "target": r[2], "confidence": r[3],
                            "context": r[4], "decay_lambda": r[5]})
        return stats, new
    finally:
        conn.close()


def _run(cmd: list, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout)


def run_miner(force: bool = False, runner=_run, extractor=None,
              now: float = None) -> dict:
    """The full opportunistic cycle. Returns a summary dict; every skip
    reason is explicit. Injectable runner/extractor for offline tests."""
    if not force and not due(now=now):
        return {"status": "skipped", "reason": "ran within the last "
                f"{MIN_HOURS_BETWEEN_RUNS}h"}
    if not ollama_up():
        return {"status": "skipped", "reason": "Ollama not running"}
    if extractor is None:
        # The scheduled path (launchd) builds its own extractor — probe it
        # end-to-end BEFORE claiming anything (Issue 9's honesty gap: a
        # server ping is not a working extractor). Injected extractors
        # (tests, callers) are the caller's responsibility.
        from src.local_parser import LocalExtractor
        extractor = LocalExtractor()
        ok, reason = extractor_ready(extractor)
        if not ok:
            return {"status": "skipped",
                    "reason": f"extractor unavailable ({reason})"}
    gcloud = _gcloud()
    if gcloud is None:
        return {"status": "skipped", "reason": "gcloud CLI not found"}

    scp_base = [gcloud, "compute", "scp", f"--project={GCP_PROJECT}",
                f"--zone={GCP_ZONE}", "--quiet"]
    ssh_base = [gcloud, "compute", "ssh", VM, f"--project={GCP_PROJECT}",
                f"--zone={GCP_ZONE}", "--quiet", "--command"]

    with tempfile.TemporaryDirectory(prefix="edge_miner.") as tmp:
        tmp = Path(tmp)
        pulled = tmp / "brain_map.db"

        # 2. PULL the live DB
        res = runner(scp_base + [f"{VM}:{VM_REPO}/data/brain_map.db",
                                 str(pulled)])
        if res.returncode != 0 or not pulled.exists():
            return {"status": "failed", "reason": "could not pull VM DB",
                    "detail": (res.stderr or "")[-300:]}

        # 3. MINE locally with Ollama
        stats, new_triples = mine_new_triples(pulled, extractor=extractor)

        # 4. APPLY the new triples on the VM (idempotent add_edge). The
        # applier travels as a FILE — multi-line python through ssh
        # --command gets newline-mangled by the remote shell (bug found
        # live in evolution.refresh_bars_cache, 2026-07-09; this path had
        # the same flaw but had never fired with >0 triples).
        applied = 0
        if new_triples:
            payload = tmp / "new_edges.json"
            payload.write_text(json.dumps(new_triples))
            applier = tmp / "apply_edges.py"
            applier.write_text(_REMOTE_APPLY)
            res = runner(scp_base + [str(payload), str(applier),
                                     f"{VM}:/tmp/"])
            if res.returncode != 0:
                return {"status": "failed", "reason": "could not ship edges",
                        "detail": (res.stderr or "")[-300:]}
            apply_cmd = (f"cd {VM_REPO} && venv/bin/python3 "
                         "/tmp/apply_edges.py /tmp/new_edges.json; "
                         "rm -f /tmp/apply_edges.py /tmp/new_edges.json")
            res = runner(ssh_base + [apply_cmd])
            if res.returncode != 0:
                return {"status": "failed", "reason": "remote apply failed",
                        "detail": (res.stderr or res.stdout or "")[-300:]}
            applied = len(new_triples)

        # 5. REFRESH the Mac's read-only copies (archive originals once)
        if not ARCHIVE_DIR.exists():
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            for name in ("brain_map.db", "journal.jsonl", "portfolio.json"):
                src = DATA_DIR / name
                if src.exists():
                    shutil.copy2(src, ARCHIVE_DIR / name)
        runner(scp_base + [f"{VM}:{VM_REPO}/data/brain_map.db",
                           f"{VM}:{VM_REPO}/data/journal.jsonl",
                           str(DATA_DIR) + "/"])

    _mark_success(now=now)
    summary = {"status": "ok", "outcomes_considered":
               stats.get("outcomes_considered"),
               "triples_written_locally": stats.get("triples_written"),
               "new_edges_applied_to_vm": applied}
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Opportunistic Ollama edge mining against the VM's "
                    "knowledge graph")
    parser.add_argument("--force", action="store_true",
                        help="ignore the 20h since-last-success gate")
    args = parser.parse_args()
    result = run_miner(force=args.force)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] edge_miner: "
          f"{json.dumps(result)}", flush=True)
