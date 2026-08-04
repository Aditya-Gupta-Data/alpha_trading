#!/bin/bash
# LaunchAgent wrapper for Procedural Evolution (src/evolution.py).
# Scheduled by scripts/com.alphatrading.evolution.plist: Saturdays 02:00
# (Mac local time = IST on this machine) — market closed, nowhere near the
# 07:00 IST token-renewal slot. The module itself fail-closes without a
# running Ollama or a bars cache, so a misfire is a quiet no-op.
#
# This runs on the MAC ONLY — the VM has no Ollama by design (decision
# #47), which is why the equivalent VM cron entry was removed from
# setup_cron.sh (Phase 5 scratchpad build).

export PATH="/opt/homebrew/bin:/Library/Frameworks/Python.framework/Versions/3.14/bin:$PATH"
cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p logs

# STRICT ON-DEMAND OLLAMA (2026-08-04, ledger Issue 23) — same policy and
# same single door as scripts/mine_edges.sh. This one matters more than it
# looks: a Saturday 02:00 run left a model resident while the owner slept.
# `exec` was deliberately REMOVED below — it would replace this shell and
# skip the trap, orphaning the server.
. scripts/ollama_session.sh
trap ollama_session_stop EXIT INT TERM
ollama_session_start || true   # evolution fail-closes without Ollama; a misfire is a quiet no-op

# Interpreter PINNED, never resolved from PATH — the standing lesson from
# THREE unpinned-interpreter incidents in 48h (Mac cron: CommandLineTools
# python; VM cron: bare python3; edge-miner LaunchAgent: package-less
# Homebrew python). Same pin as scripts/mine_edges.sh.
/Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m src.evolution >> logs/evolution.log 2>&1
_rc=$?
ollama_session_stop
exit $_rc
