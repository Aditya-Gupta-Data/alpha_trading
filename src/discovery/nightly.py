"""
src/discovery/nightly.py — the GATED nightly discovery pass (decision #76)
==========================================================================

The single cron entry that lets the Phase-5 miners run unattended. The
miners themselves (src/discovery/run_miners.py) stay deliberately dumb —
enumerate + register CANDIDATEs, nothing surfaces without the proving
harness — so the only real risks of automation are OPERATIONAL, and this
wrapper gates exactly those:

  HEALTH GATE   Mining on a broken ingestion day is garbage-in: the day's
                daily_context frame may be missing or degraded, and every
                junk hypothesis tested inflates the Benjamini-Hochberg
                denominator. Gate on the SAME two signals the dashboard's
                health map reads — ops_monitor.check_heartbeats() (a
                silent scheduled job today = RED, caught in REAL TIME) and
                the LATEST ops sweep's slice of logs/problems.jsonl
                restricted to the INGESTION logs (the miners' upstream; a
                fail-open note in, say, the master scheduler does not
                block discovery). "Latest sweep" because this job runs at
                20:20, BEFORE tonight's 20:30 sweep — same convention as
                every other heartbeat-monitored job (all fire pre-sweep,
                or the next day's sweep would false-flag them "silent").
                The trade: an error LINE logged by today's 18:50-19:45
                ingestion is only swept tonight, so it gates TOMORROW's
                pass, not tonight's — one-night exposure to soft-degraded
                frames whose absent readings are NULL-honest anyway
                (missing field -> no ctx tag, never a wrong one), while a
                job that silently DIED today blocks tonight immediately.
                Deliberately called in-process, NOT via the dashboard's
                HTTP endpoint: same truth, no api_server/transport
                dependency from cron.
  DEPTH GATE    The panel rule that kept run_miners manual-only, as a
                number: below MIN_CONTEXT_FRAMES daily_context rows a
                lagged itemset can't plausibly clear the support floor,
                so every run legitimately finds nothing and a nightly
                "0 survivors" card just trains the owner to ignore the
                surface. Skip until the history exists.
  ANTI-SILENT-DEATH  A skip is one log line + exit 0 (cron-quiet), but
                every NOTIFY_EVERY_SKIPS-th consecutive skip fires one
                Discord note — the gate itself must never become the
                thing that silently dies. State in a tiny ledger file.

Isolation: imports nothing from news_parser / text_intelligence (the
miners read daily_context + brain_map tables only). Fail-open everywhere;
this wrapper never raises out of run_nightly().

Cron (VM): 20:20 IST daily — after the 20:00 sleep phase (drift-monitor
Task H has run) and BEFORE the 20:30 ops sweep, so its own heartbeat is
checkable like every other monitored job. Installed by
scripts/setup_cron.sh job #18; heartbeat-monitored via
discovery_nightly.log in ops_monitor.EXPECTED_JOBS.

Manual:  python3 -m src.discovery.nightly
"""

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LOGS_DIR = ROOT / "logs"
STATE_PATH = LOGS_DIR / ".discovery_nightly_state.json"

MIN_CONTEXT_FRAMES = 60      # the depth floor (the panel rule as a number)
NOTIFY_EVERY_SKIPS = 7       # one Discord note per 7 consecutive skips

# The miners' upstream — the Data-department logs whose problem lines mean
# "today's frames may be degraded or missing". Problem lines elsewhere
# (scheduler, tracker, renewals) are real but not discovery's business.
INGESTION_LOGS = frozenset({
    "news_processor.log", "rss_ingester.log", "deals_tracker.log",
    "flows_tracker.log", "earnings_calendar.log", "daily_archiver.log",
    "chain_archiver.log",
})


# ----------------------------------------------------------- skip ledger

def _load_state(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


def _save_state(path: Path, state: dict) -> None:
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2))
    except OSError as exc:
        print(f"  (discovery nightly: could not save state [{exc}])")


# ----------------------------------------------------------------- gates

def health_gate(logs_dir: Path = None, now: datetime = None,
                expected: dict = None) -> dict:
    """The dashboard health map's own signals, read in-process.
    {ok, silent_jobs, ingestion_problems}. Fail-CLOSED on the signals
    themselves being unreadable? No — check_heartbeats/ledger reads are
    local file stats; if THEY break something is wrong enough to skip."""
    from src import ops_monitor
    logs_dir = Path(logs_dir) if logs_dir is not None else LOGS_DIR
    now = now or datetime.now()

    # Our own log is excluded — tonight's run must not self-flag before
    # it has happened.
    exp = dict(expected if expected is not None
               else (ops_monitor._expected_jobs_from_env()
                     or ops_monitor.EXPECTED_JOBS))
    exp.pop("discovery_nightly.log", None)
    try:
        silent = ops_monitor.check_heartbeats(logs_dir, now=now, expected=exp)
    except Exception as exc:
        return {"ok": False, "silent_jobs": [f"(heartbeat check failed: {exc})"],
                "ingestion_problems": 0}

    # The LATEST sweep's slice (this job runs pre-sweep at 20:20, so the
    # freshest ledger entries are yesterday's 20:30 sweep — see module
    # docstring for the one-night trade this makes deliberately).
    problems = 0
    try:
        ledger = Path(logs_dir) / "problems.jsonl"
        records = []
        if ledger.exists():
            for raw in ledger.read_text().splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    records.append(json.loads(raw))
                except ValueError:
                    continue
        dates = [str(r.get("found", ""))[:10] for r in records]
        latest = max((d for d in dates if d), default=None)
        if latest:
            problems = sum(int(r.get("count", 1)) for r, d in zip(records, dates)
                           if d == latest and r.get("log") in INGESTION_LOGS)
    except Exception as exc:
        return {"ok": False, "silent_jobs": [],
                "ingestion_problems": -1,
                "error": f"problem ledger unreadable: {exc}"}

    return {"ok": not silent and problems == 0,
            "silent_jobs": silent, "ingestion_problems": problems}


def depth_gate(conn, min_frames: int = MIN_CONTEXT_FRAMES) -> dict:
    """{ok, frames}: is the daily_context series deep enough that mining
    can plausibly clear the support floors? Missing table -> 0 frames."""
    from src import daily_context as dc
    try:
        dc.ensure_schema(conn)
        frames = conn.execute(
            "SELECT COUNT(*) FROM daily_context").fetchone()[0]
    except Exception:
        frames = 0
    return {"ok": frames >= min_frames, "frames": frames,
            "min_frames": min_frames}


# ------------------------------------------------------------- the pass

def _default_notify(text: str) -> None:
    """One-line Discord note (the ops_monitor pattern): fail-open, and the
    notifier muzzles itself under pytest (Phase 6J guard)."""
    try:
        import asyncio
        from src.notifier import send_discord_message
        asyncio.run(send_discord_message(text))
    except Exception as exc:
        print(f"  (discovery nightly: notify failed [{exc}])")


def run_nightly(conn=None, logs_dir: Path = None, now: datetime = None,
                min_frames: int = MIN_CONTEXT_FRAMES,
                state_path: Path = None, notify_fn=None,
                run_fn=None, expected: dict = None) -> dict:
    """Gate, then mine. Returns {ran, gates, consecutive_skips, report?}.
    Never raises; a skip is a normal, quiet outcome (exit 0 at the CLI)."""
    now = now or datetime.now()
    state_path = Path(state_path) if state_path is not None else STATE_PATH
    notify_fn = notify_fn or _default_notify

    own = conn is None
    if conn is None:
        from src import brain_map
        conn = brain_map.connect()
    try:
        health = health_gate(logs_dir, now=now, expected=expected)
        depth = depth_gate(conn, min_frames=min_frames)
        gates = {"health": health, "depth": depth}

        state = _load_state(state_path)
        if not (health["ok"] and depth["ok"]):
            skips = int(state.get("consecutive_skips", 0)) + 1
            _save_state(state_path, {"consecutive_skips": skips,
                                     "last_skip": now.isoformat(timespec="seconds")})
            reasons = []
            if health["silent_jobs"]:
                reasons.append(f"{len(health['silent_jobs'])} silent job(s): "
                               + ", ".join(health["silent_jobs"][:4]))
            if health["ingestion_problems"]:
                reasons.append(f"{health['ingestion_problems']} ingestion "
                               "problem line(s) today")
            if not depth["ok"]:
                reasons.append(f"daily_context {depth['frames']}/"
                               f"{depth['min_frames']} frames")
            line = ("(discovery nightly: SKIPPED — " + "; ".join(reasons)
                    + f" — {skips} consecutive)")
            print(line, flush=True)
            if skips % NOTIFY_EVERY_SKIPS == 0:
                notify_fn(f"⛔ **Discovery pass has skipped {skips} nights "
                          f"in a row** — {'; '.join(reasons)}. The gate is "
                          "doing its job, but check whether the underlying "
                          "condition (ingestion health / context depth) is "
                          "being worked on.")
            return {"ran": False, "gates": gates, "consecutive_skips": skips}

        from src.discovery import run_miners
        report = run_miners.run_all(conn=conn, today=now.date())
        _save_state(state_path, {"consecutive_skips": 0,
                                 "last_run": now.isoformat(timespec="seconds")})
        print(f"(discovery nightly: RAN — {report['summary']})", flush=True)
        return {"ran": True, "gates": gates, "consecutive_skips": 0,
                "report": report}
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    result = run_nightly()
    # Skips are normal (exit 0) — cron must stay quiet; the skip ledger and
    # the every-7th Discord note are the escalation path, not cron mail.
    print(json.dumps({k: v for k, v in result.items() if k != "report"},
                     indent=2, default=str))
