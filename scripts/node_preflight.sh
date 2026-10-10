#!/bin/bash
# scripts/node_preflight.sh — is the HOME NODE ready for its cron block?
# ==============================================================================
# MANUAL OFFLINE TOOL. Read-only: it writes nothing, installs nothing, and
# never touches a token. Run it on the Mini PC after docs/HOME_NODE_SETUP.md
# and before `setup_mininode_cron.sh --shadow`. Every line is PASS / WARN /
# FAIL with the fix spelled out; the exit code is 1 on any FAIL.
#
#   bash scripts/node_preflight.sh            # full check, including one read-only ssh to the VM
#   bash scripts/node_preflight.sh --no-vm    # skip the VM round-trip (no network / no gcloud yet)
#
# What it checks, in the order the setup guide installs them: OS + clock,
# no-sleep + cron, the repo, the venv interpreter + imports, the corpus
# copied from the Mac, the .env (present, private, NO Dhan account-control
# keys — decision #46), gcloud (logged in, right project, VM reachable),
# Ollama (optional), and whether a cron block is already installed.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1
NO_VM=0
[ "${1:-}" = "--no-vm" ] && NO_VM=1

PASS=0; WARN=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  PASS  %s\n' "$*"; }
warn() { WARN=$((WARN+1)); printf '  WARN  %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$*"; }
section() { printf '\n%s\n' "$*"; }

echo "HOME NODE PREFLIGHT — $(hostname) — $(date '+%Y-%m-%d %H:%M %Z')"

# ------------------------------------------------------------ 1. OS + clock
section "1. OS and clock"
case "$(uname -s)" in
    Linux) ok "Linux ($(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || uname -r))" ;;
    *) bad "this is $(uname -s), not Linux — the home node is the Ubuntu box" ;;
esac
case "$(hostname)" in
    alpha-trading-vm*) bad "hostname looks like the VM — never install the node block there" ;;
    *) ok "hostname $(hostname) is not the VM" ;;
esac
if [ "$(date +%z)" = "+0530" ]; then
    ok "clock is IST (+0530)"
else
    bad "clock offset is $(date +%z), not +0530 — run: sudo timedatectl set-timezone Asia/Kolkata"
fi
if command -v timedatectl >/dev/null 2>&1; then
    if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes; then
        ok "NTP synchronised"
    else
        warn "NTP not synchronised yet — run: sudo timedatectl set-ntp true (cron fires on wall-clock time)"
    fi
fi

# ------------------------------------------------------ 2. never sleep, cron
section "2. Never sleep, and cron"
if command -v systemctl >/dev/null 2>&1; then
    unmasked=""
    for t in sleep.target suspend.target hibernate.target hybrid-sleep.target; do
        [ "$(systemctl is-enabled "$t" 2>/dev/null)" = "masked" ] || unmasked="$unmasked $t"
    done
    if [ -z "$unmasked" ]; then
        ok "sleep/suspend/hibernate targets masked"
    else
        bad "not masked:$unmasked — run: sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target"
    fi
    if grep -qE '^IdleAction=ignore' /etc/systemd/logind.conf 2>/dev/null; then
        ok "logind IdleAction=ignore"
    else
        warn "logind IdleAction not set to ignore (Ubuntu Server's default is already ignore; set it explicitly to be safe — HOME_NODE_SETUP.md §3)"
    fi
    if [ "$(systemctl is-active cron 2>/dev/null)" = "active" ]; then
        ok "cron service active"
    else
        bad "cron is not active — run: sudo apt install -y cron && sudo systemctl enable --now cron"
    fi
else
    warn "no systemctl — cannot verify sleep masking or the cron service"
fi
echo "        (BIOS 'Restore on AC Power Loss = Power On' cannot be checked from here — confirm it by hand)"

# -------------------------------------------------------------- 3. the repo
section "3. The repo"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
    ok "git checkout at $(git rev-parse --short HEAD 2>/dev/null) on branch $branch"
    if [ "$REPO_ROOT" != "$HOME/alpha_trading" ]; then
        warn "repo is at $REPO_ROOT, not ~/alpha_trading — fine, but the cron block hardcodes this path; do not move it later"
    fi
    if [ -n "$(git status --porcelain 2>/dev/null | grep -v '^??')" ]; then
        warn "tracked files are modified on the node — the node should only ever git pull, never edit"
    fi
    if ! grep -q -- '--shadow' scripts/setup_mininode_cron.sh 2>/dev/null; then
        bad "this checkout predates the shadow trial (no --shadow in setup_mininode_cron.sh) — git pull the branch that carries decision #143"
    else
        ok "installer carries --shadow (decision #143)"
    fi
else
    bad "$REPO_ROOT is not a git checkout"
fi

# ----------------------------------------------------- 4. the interpreter
section "4. Python"
. scripts/node_env.sh
if [ "$PY_SOURCE" = "repo venv" ] || [ "$PY_SOURCE" = "ALPHA_PY" ]; then
    ok "interpreter: $PY ($PY_SOURCE)"
else
    bad "interpreter resolved to '$PY_SOURCE' ($PY) — create the venv: python3 -m venv venv && venv/bin/pip install -r requirements.txt"
fi
pyver="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 0.0)"
if "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    ok "python $pyver (>= 3.12)"
else
    bad "python $pyver is older than 3.12 — Ubuntu 24.04 ships 3.12; on 22.04 install python3.12 and rebuild the venv"
fi
missing="$("$PY" - <<'PYEOF' 2>/dev/null
import importlib
mods = ["yfinance", "pandas", "networkx", "sklearn", "dhanhq", "pyotp", "discord", "yaml", "httpx", "fastapi"]
print(" ".join(m for m in mods if importlib.util.find_spec(m) is None))
PYEOF
)"
if [ -z "$missing" ]; then
    ok "requirements importable (yfinance, pandas, networkx, sklearn, dhanhq, pyotp, discord, yaml, httpx, fastapi)"
else
    bad "missing python packages: $missing — run: venv/bin/pip install -r requirements.txt"
fi
if "$PY" -c 'import src.config' >/dev/null 2>&1; then
    ok "src.config imports"
else
    bad "src.config does not import — run: $PY -c 'import src.config' and read the error"
fi

# ------------------------------------------------------------ 5. the corpus
section "5. The corpus copied from the Mac (data/ is gitignored)"
n_results="$(find data/lake/financial_results -maxdepth 1 -name '*.json' 2>/dev/null | wc -l | tr -d ' ')"
if [ "$n_results" -gt 0 ]; then
    ok "data/lake/financial_results: $n_results result files (the valuation pass reads these)"
else
    bad "data/lake/financial_results is empty or absent — without it valuation scores 0 of 109 darlings; rsync data/ from the Mac (HOME_NODE_SETUP.md §6)"
fi
[ -s data/darlings_queue.json ] && ok "data/darlings_queue.json present" || bad "data/darlings_queue.json missing — part of the Mac's data/ copy"
[ -s config/sector_universe.json ] && ok "config/sector_universe.json present (tracked)" || bad "config/sector_universe.json missing — the checkout is incomplete"
if [ -s data/bars_cache.json ]; then
    ok "data/bars_cache.json present"
else
    warn "data/bars_cache.json absent — the sync refreshes it through the VM on its first run (needs gcloud)"
fi
n_fo="$(find data/lake/fo_bhavcopy -type f 2>/dev/null | wc -l | tr -d ' ')"
if [ "$n_fo" -gt 0 ]; then
    ok "data/lake/fo_bhavcopy: $n_fo files"
else
    warn "data/lake/fo_bhavcopy empty — the sync fetches the last 5 weekdays from NSE on its first run"
fi
if command -v df >/dev/null 2>&1; then
    free_gb="$(df -BG --output=avail "$REPO_ROOT" 2>/dev/null | tail -1 | tr -dc '0-9')"
    [ -n "$free_gb" ] && { [ "$free_gb" -ge 10 ] && ok "disk: ${free_gb} GB free" || warn "disk: only ${free_gb} GB free"; }
fi

# --------------------------------------------------------------- 6. the .env
section "6. The .env"
if [ -f .env ]; then
    ok ".env present"
    perms="$(stat -c %a .env 2>/dev/null || stat -f %Lp .env 2>/dev/null)"
    [ "$perms" = "600" ] && ok ".env is private (600)" || warn ".env mode is $perms — run: chmod 600 .env"
    leaked="$(grep -oE '^(DHAN_PIN|DHAN_TOTP_SECRET|DHAN_API_KEY|DHAN_API_SECRET)=' .env | tr -d '=' | tr '\n' ' ')"
    if [ -z "$leaked" ]; then
        ok "no Dhan account-control keys in .env (decision #46)"
    else
        bad "Dhan account-control keys present: $leaked— the node needs none of them. Run: sed -i '/^DHAN_PIN=/d;/^DHAN_TOTP_SECRET=/d;/^DHAN_API_KEY=/d;/^DHAN_API_SECRET=/d' .env"
    fi
    grep -qE '^DISCORD_WEBHOOK_URL=.+' .env && ok "DISCORD_WEBHOOK_URL set (Saturday scrip card; silent in shadow)" \
        || warn "DISCORD_WEBHOOK_URL not set — the Saturday scrip card cannot post after promotion"
else
    bad ".env missing — scp it from the Mac, then strip the four DHAN_ account-control keys (HOME_NODE_SETUP.md §6)"
fi

# ---------------------------------------------------------------- 7. gcloud
section "7. gcloud — the one lane to the VM"
GC="$(command -v gcloud || true)"
if [ -z "$GC" ]; then
    bad "gcloud not found — install google-cloud-cli (HOME_NODE_SETUP.md §7)"
else
    ok "gcloud at $GC"
    acct="$("$GC" auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1)"
    if [ -n "$acct" ]; then
        ok "logged in as $acct"
        case "$acct" in
            adigupta1998@*) ;;
            *) warn "account is not adigupta1998@… — the scripts ssh as adigupta1998@alpha-trading-vm; a different Google account maps to a different VM user" ;;
        esac
    else
        bad "no active gcloud account — run: gcloud auth login --no-browser"
    fi
    proj="$("$GC" config get-value project 2>/dev/null)"
    if [ "$proj" = "project-37632031-10d0-47dd-b6f" ]; then
        ok "project project-37632031-10d0-47dd-b6f"
    else
        bad "gcloud project is '${proj:-unset}' — run: gcloud config set project project-37632031-10d0-47dd-b6f"
    fi
    [ -n "${CLOUDSDK_PYTHON:-}" ] && ok "CLOUDSDK_PYTHON pinned to $CLOUDSDK_PYTHON (node_env.sh)" || warn "CLOUDSDK_PYTHON not pinned"
    if [ "$NO_VM" -eq 1 ]; then
        warn "VM round-trip skipped (--no-vm)"
    elif [ -n "$acct" ]; then
        if out="$(timeout 90 "$GC" compute ssh adigupta1998@alpha-trading-vm --project=project-37632031-10d0-47dd-b6f --zone=us-central1-a --quiet --command='hostname' 2>&1)" && echo "$out" | grep -q alpha-trading-vm; then
            ok "VM reachable over gcloud compute ssh (read-only hostname check)"
        else
            bad "could not ssh to the VM — $(echo "$out" | tail -1 | cut -c1-160). First contact registers the key: gcloud compute ssh adigupta1998@alpha-trading-vm --zone=us-central1-a --command=hostname"
        fi
    fi
fi

# ---------------------------------------------------------------- 8. Ollama
section "8. Ollama (optional — the miner and evolution fail open without it)"
if command -v ollama >/dev/null 2>&1; then
    ok "ollama at $(command -v ollama)"
    model="${OLLAMA_MODEL:-$(grep -E '^OLLAMA_MODEL=' .env 2>/dev/null | cut -d= -f2-)}"
    model="${model:-llama3}"
    if ollama list 2>/dev/null | awk '{print $1}' | grep -q "^${model%%:*}"; then
        ok "model '$model' pulled"
    else
        warn "model '$model' not pulled — run: ollama pull $model"
    fi
    if [ "$(systemctl is-enabled ollama 2>/dev/null)" = "enabled" ]; then
        warn "ollama.service is enabled at boot — the scripts start/stop it per job (ledger Issue 23): sudo systemctl disable --now ollama"
    else
        ok "ollama.service not enabled at boot (on-demand per job)"
    fi
else
    warn "ollama not installed — edge miner and evolution will skip; fine for the trial"
fi
if command -v free >/dev/null 2>&1; then
    mem_gb="$(free -g | awk '/^Mem:/{print $2}')"
    [ -n "$mem_gb" ] && { [ "$mem_gb" -ge 7 ] && ok "RAM: ${mem_gb} GB" || warn "RAM: ${mem_gb} GB — keep Ollama at a 3B model (OLLAMA_MODEL=llama3.2:3b)"; }
fi

# ------------------------------------------------------- 9. the cron block
section "9. The cron block"
if crontab -l 2>/dev/null | grep -q 'ALPHA TRADING HOME NODE BLOCK START'; then
    if crontab -l 2>/dev/null | grep -q '^ALPHA_NODE_SHADOW=1'; then
        ok "home node block installed in SHADOW mode (ships nothing)"
    elif crontab -l 2>/dev/null | grep -q '^ALPHA_NODE_SHADOW=0'; then
        ok "home node block installed in LIVE mode (this node owns the lane — the Mac's agents must be retired)"
    else
        warn "home node block installed but predates the shadow switch — re-run: bash scripts/setup_mininode_cron.sh --shadow"
    fi
    crontab -l 2>/dev/null | grep -q '^@reboot' && ok "@reboot catch-up lines present" || warn "no @reboot catch-up lines — re-run the installer"
    crontab -l 2>/dev/null | grep -q 'weekly_recalibration' && bad "weekly_recalibration is still in the node's crontab — it moved to the VM (cron #36); re-run the installer" || ok "no weekly_recalibration on the node (VM cron #36 owns it)"
else
    warn "no home node block installed yet — when every FAIL above is gone: bash scripts/setup_mininode_cron.sh --shadow"
fi
crontab -l 2>/dev/null | grep -qE 'renew_token|push_token_to_vm|setup_cron.sh' && bad "the crontab carries a token or VM job — remove it; the node never renews or pushes a token (decision #48)"

# ------------------------------------------------------------------ summary
section "SUMMARY: $PASS pass, $WARN warn, $FAIL fail"
if [ "$FAIL" -eq 0 ]; then
    echo "  READY. Next: bash scripts/setup_mininode_cron.sh --shadow, then one hand run:"
    echo "         ALPHA_NODE_SHADOW=1 bash scripts/mac_auto_sync.sh --force && tail -3 logs/mac_auto_sync.log"
    exit 0
else
    echo "  NOT READY — fix every FAIL line above (each names its command), then run this again."
    exit 1
fi
