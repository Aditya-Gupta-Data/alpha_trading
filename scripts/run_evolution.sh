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
# Interpreter PINNED, never resolved from PATH — the standing lesson from
# THREE unpinned-interpreter incidents in 48h (Mac cron: CommandLineTools
# python; VM cron: bare python3; edge-miner LaunchAgent: package-less
# Homebrew python). Same pin as scripts/mine_edges.sh.
exec /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m src.evolution >> logs/evolution.log 2>&1
