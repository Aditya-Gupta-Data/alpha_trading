#!/bin/bash
# run_dashboard_backend.sh — Mac-local backend for the Lovable dashboard.
#
# Serves src.api on 127.0.0.1:8000 in OPEN (key-optional) mode, with the
# deployed Lovable frontend origin CORS-allowed via EXTRA_CORS_ORIGINS, so
# the ngrok tunnel (com.alphatrading.dashboard-tunnel) can expose it to
# https://adi-trader-zen.lovable.app. Started at login and kept alive by
# the com.alphatrading.dashboard-backend LaunchAgent.
#
# Touches NO VM — this is the observation-week-safe "let me see the
# dashboard on my phone while the laptop is open" path. The interpreter is
# pinned to the absolute Framework python that has fastapi/uvicorn (bare
# `python3` from a launchd PATH resolves to a package-less Homebrew python
# — the lesson from the edge-miner/evolution launchers).
cd /Users/adityagupta/Documents/Claude/alpha_trading || exit 1
export EXTRA_CORS_ORIGINS="https://adi-trader-zen.lovable.app"
unset API_KEY
exec /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
    -m uvicorn src.api:app --host 127.0.0.1 --port 8000
