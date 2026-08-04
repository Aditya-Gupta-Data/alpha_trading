#!/bin/bash
# ollama_session.sh — the ONE on/off switch for the local Ollama server.
#
# Sourced (never executed) by scripts/mine_edges.sh and
# scripts/run_evolution.sh. House convention "one door per concern": both
# LLM-using LaunchAgents open and close Ollama through here, so the
# lifetime policy lives in exactly one file.
#
# WHY THIS EXISTS (2026-08-04, ledger Issue 23). Ollama.app registers
# itself as an SMAppService background item and had been serving 24/7
# since 7 Jul. A previous `launchctl setenv OLLAMA_KEEP_ALIVE 0` fix
# lapsed silently — `setenv` does not survive reboot, and it never
# reached the already-running server anyway (verified: `ps -E` on the
# live pid showed no OLLAMA_KEEP_ALIVE). So every scheduled run left a
# ~2GB model resident for the default 5-minute keep-alive on an 8GB Mac.
#
# THE POLICY: the server exists only for the duration of a scheduled job.
# Start it, wait until it actually answers, run the job, kill it dead.
#
# LEAVE-NO-TRACE, BOTH WAYS. If a server is ALREADY reachable when we
# start, that is the owner's own session (they opened Ollama.app by
# hand). We use it and we DO NOT kill it on the way out. "No trace"
# means the machine is left exactly as we found it — not that we
# terminate processes we did not start.

# Pinned, never resolved from PATH — the standing lesson from the three
# unpinned-interpreter incidents (see mine_edges.sh). This is a symlink
# into Ollama.app, and it runs fine with the app itself never launched,
# which is the entire point of disabling the background item.
OLLAMA_BIN="${OLLAMA_BIN:-/usr/local/bin/ollama}"

# Loopback only. An on-demand server must never be reachable off-box.
export OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"

# Defence in depth #1 (the server side). local_parser also sends
# keep_alive=0 per request, so the unload happens even if this env is
# somehow lost — the exact failure mode that made this issue recur.
export OLLAMA_KEEP_ALIVE=0
export OLLAMA_MAX_LOADED_MODELS=1
export OLLAMA_NUM_PARALLEL=1
# The KV cache grows with context and is what actually pushes an 8GB Mac
# into swap — capping this matters as much as unloading the weights.
export OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-4096}"

_OLLAMA_SESSION_PID=""
_OLLAMA_SESSION_OWNED=0
_OLLAMA_SESSION_LOG="logs/ollama_session.log"

_ollama_log() {
    mkdir -p "$(dirname "$_OLLAMA_SESSION_LOG")" 2>/dev/null
    echo "$(date '+%Y-%m-%d %H:%M:%S') [ollama_session] $*" \
        >> "$_OLLAMA_SESSION_LOG"
}

_ollama_reachable() {
    curl -sf -o /dev/null --max-time 2 "http://${OLLAMA_HOST}/api/tags"
}

# Start a server we own, or adopt one we don't. Returns 0 when a server
# is answering, 1 when we could not get one up — callers must treat 1 as
# "skip the LLM work", never as a fatal error: every consumer of this
# (edge_miner, evolution, local_parser) already fail-opens without
# Ollama, exactly as the VM does by design (decision #47).
ollama_session_start() {
    if _ollama_reachable; then
        _OLLAMA_SESSION_OWNED=0
        _ollama_log "server already up (owner's own session) — adopting, will NOT kill on exit"
        return 0
    fi

    if [ ! -x "$OLLAMA_BIN" ]; then
        _ollama_log "FAIL: no ollama binary at $OLLAMA_BIN — skipping LLM work"
        return 1
    fi

    "$OLLAMA_BIN" serve >> "$_OLLAMA_SESSION_LOG" 2>&1 &
    _OLLAMA_SESSION_PID=$!
    _OLLAMA_SESSION_OWNED=1
    _ollama_log "started ollama serve pid=$_OLLAMA_SESSION_PID (keep_alive=0, ctx=$OLLAMA_CONTEXT_LENGTH)"

    # Wait for READY, not for a sleep. Issue 9's standing lesson: a
    # process that exists is not a service that answers.
    local waited=0
    while [ "$waited" -lt 30 ]; do
        if _ollama_reachable; then
            _ollama_log "ready after ${waited}s"
            return 0
        fi
        # Died during startup (port already taken by something else,
        # corrupt model dir) — stop waiting on a corpse.
        if ! kill -0 "$_OLLAMA_SESSION_PID" 2>/dev/null; then
            _ollama_log "FAIL: server exited during startup — skipping LLM work"
            _OLLAMA_SESSION_PID=""
            _OLLAMA_SESSION_OWNED=0
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done

    _ollama_log "FAIL: not ready after ${waited}s — killing and skipping LLM work"
    ollama_session_stop
    return 1
}

# Kill the server we started, and its model runners with it. Idempotent
# and safe to call from a trap on any exit path.
ollama_session_stop() {
    if [ "$_OLLAMA_SESSION_OWNED" != "1" ] || [ -z "$_OLLAMA_SESSION_PID" ]; then
        [ "$_OLLAMA_SESSION_OWNED" = "0" ] && [ -n "$_OLLAMA_SESSION_LOG" ] && \
            _ollama_log "nothing to stop (adopted or never started)"
        return 0
    fi

    # `ollama serve` forks an `ollama runner` child per loaded model.
    # Collect the children BEFORE killing the parent, or they are
    # reparented to launchd and left holding the model's RAM.
    local kids
    kids="$(pgrep -P "$_OLLAMA_SESSION_PID" 2>/dev/null | tr '\n' ' ')"

    kill -TERM "$_OLLAMA_SESSION_PID" 2>/dev/null
    local waited=0
    while [ "$waited" -lt 10 ] && kill -0 "$_OLLAMA_SESSION_PID" 2>/dev/null; do
        sleep 1
        waited=$((waited + 1))
    done
    if kill -0 "$_OLLAMA_SESSION_PID" 2>/dev/null; then
        _ollama_log "pid=$_OLLAMA_SESSION_PID ignored TERM after ${waited}s — SIGKILL"
        kill -KILL "$_OLLAMA_SESSION_PID" 2>/dev/null
    fi
    wait "$_OLLAMA_SESSION_PID" 2>/dev/null

    # Sweep any runner that outlived its parent. Scoped to the children
    # we recorded — never a blanket `pkill ollama`, which would murder
    # the owner's GUI session if they opened it mid-run.
    local k
    for k in $kids; do
        if kill -0 "$k" 2>/dev/null; then
            _ollama_log "reaping orphaned runner pid=$k"
            kill -KILL "$k" 2>/dev/null
        fi
    done

    if _ollama_reachable; then
        _ollama_log "WARNING: port ${OLLAMA_HOST} still answering after stop — another server is running"
    else
        _ollama_log "stopped cleanly (pid=$_OLLAMA_SESSION_PID), port released"
    fi

    _OLLAMA_SESSION_PID=""
    _OLLAMA_SESSION_OWNED=0
}
