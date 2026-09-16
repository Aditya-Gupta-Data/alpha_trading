#!/bin/bash
# scripts/node_env.sh — ONE interpreter/PATH resolver for the HOME NODE scripts
# ==============================================================================
# Sourced (not run) by mac_auto_sync.sh, mine_edges.sh and run_evolution.sh.
#
# WHY (2026-09-16, decision #99). Those three scripts pinned the interpreter
# to the Mac's framework python by absolute path — the standing lesson from
# three unpinned-interpreter incidents. The home-node role now moves to an
# always-on Linux Mini PC, where that path does not exist. The pin stays a
# pin: this file resolves ONE explicit interpreter, in a fixed order, and
# never a bare `python3` off cron's PATH:
#
#   1. $ALPHA_PY, when the owner sets it (an explicit choice outranks ours)
#   2. <repo>/venv/bin/python        — the node's own venv (Linux/Mini PC)
#   3. the Mac framework python 3.14 — the laptop, unchanged
#   4. command -v python3            — last resort, and it says so in the log
#
# It also widens PATH for gcloud/ollama on either OS. It never renews a
# token and never installs anything.
_NODE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -n "${ALPHA_PY:-}" ] && [ -x "${ALPHA_PY}" ]; then
    PY="$ALPHA_PY"; PY_SOURCE="ALPHA_PY"
elif [ -x "$_NODE_ROOT/venv/bin/python" ]; then
    PY="$_NODE_ROOT/venv/bin/python"; PY_SOURCE="repo venv"
elif [ -x "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3" ]; then
    PY="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"; PY_SOURCE="mac framework"
else
    PY="$(command -v python3 || true)"; PY_SOURCE="PATH fallback"
fi
export PY PY_SOURCE

# gcloud + ollama on macOS (Homebrew) and Linux (apt / snap / google tarball)
export PATH="/opt/homebrew/bin:/opt/homebrew/share/google-cloud-sdk/bin:\
/usr/local/bin:/usr/bin:/bin:/snap/bin:$HOME/google-cloud-sdk/bin:\
/usr/lib/google-cloud-sdk/bin:/usr/local/google-cloud-sdk/bin:$PATH"

# gcloud is a shell wrapper that goes looking for a python of its own; pin
# it to ours so an unattended run never picks an unsupported system python
# (the 2026-07-21 → 08-05 silent-ship-failure lesson, config.gcloud_env).
[ -z "${CLOUDSDK_PYTHON:-}" ] && [ -n "$PY" ] && export CLOUDSDK_PYTHON="$PY"

node_env_describe() {
    echo "py=$PY ($PY_SOURCE) gcloud=$(command -v gcloud || echo none) ollama=$(command -v ollama || echo none) host=$(hostname) os=$(uname -s)"
}
