#!/bin/bash
# LaunchAgent wrapper for the opportunistic edge miner (src/edge_miner.py).
# Runs at Mac login + once daily at 21:00; the miner itself decides whether
# it's actually due (>20h since last success, Ollama up, gcloud present) and
# skips silently otherwise — so firing this often is free.

export PATH="/opt/homebrew/share/google-cloud-sdk/bin:/opt/homebrew/bin:/Library/Frameworks/Python.framework/Versions/3.14/bin:$PATH"
cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p logs
exec python3 -m src.edge_miner >> logs/edge_miner.log 2>&1
