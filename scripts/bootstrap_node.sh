#!/bin/bash
# scripts/bootstrap_node.sh — runs ON THE MINI PC: build the home node end to end
# ==============================================================================
# MANUAL OFFLINE TOOL (2026-10-10, decision #143). Normally launched by
# scripts/bootstrap_node_from_mac.sh, which first rsyncs the Mac's checkout
# + data/ + .env to ~/alpha_trading on the node and then runs this over
# `ssh -t`, so sudo and the gcloud login can prompt on your terminal.
#
# Idempotent: every step checks before it acts, so re-running after a fix is
# safe. It NEVER renews or pushes a Dhan token, never runs setup_cron.sh,
# and strips the four Dhan account-control keys from the node's .env
# (decision #46). The last two steps — the read-only preflight and the
# SHADOW cron install — happen only when the preflight says READY.
#
#   bash scripts/bootstrap_node.sh                # full build + shadow install
#   bash scripts/bootstrap_node.sh --with-ollama  # also install Ollama + pull the model
#   bash scripts/bootstrap_node.sh --no-install   # stop after the preflight, install nothing in cron
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1
WITH_OLLAMA=0; NO_INSTALL=0
for a in "$@"; do
    case "$a" in
        --with-ollama) WITH_OLLAMA=1 ;;
        --no-install)  NO_INSTALL=1 ;;
        *) echo "usage: $0 [--with-ollama] [--no-install]" >&2; exit 2 ;;
    esac
done
step() { printf '\n==> %s\n' "$*"; }

if [ "$(uname -s)" = "Darwin" ]; then
    echo "bootstrap_node: this runs ON THE MINI PC. From the Mac use scripts/bootstrap_node_from_mac.sh user@node-ip" >&2
    exit 1
fi
case "$(hostname)" in
    alpha-trading-vm*) echo "bootstrap_node: refusing to run on the VM." >&2; exit 1 ;;
esac

# ------------------------------------------------------------ 1. clock
step "Clock → IST, NTP on"
if [ "$(date +%z)" != "+0530" ]; then sudo timedatectl set-timezone Asia/Kolkata; fi
sudo timedatectl set-ntp true || true
echo "    $(date '+%Y-%m-%d %H:%M %Z (%z)')"

# ------------------------------------------------------------ 2. packages
step "Packages (git, python3-venv, cron, rsync, ufw …)"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3 python3-venv python3-pip cron openssh-server rsync curl ca-certificates ufw apt-transport-https gnupg unattended-upgrades >/dev/null
echo "    done"

# ------------------------------------------------------------ 3. appliance
step "Never sleep, cron on, SSH-only firewall, no automatic reboots"
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target 2>/dev/null || true
if grep -qE '^#?IdleAction=' /etc/systemd/logind.conf; then
    sudo sed -i 's/^#\?IdleAction=.*/IdleAction=ignore/' /etc/systemd/logind.conf
else
    echo 'IdleAction=ignore' | sudo tee -a /etc/systemd/logind.conf >/dev/null
fi
sudo systemctl enable --now cron >/dev/null 2>&1
sudo ufw allow OpenSSH >/dev/null 2>&1 || true
sudo ufw --force enable >/dev/null 2>&1 || true
if [ -f /etc/apt/apt.conf.d/50unattended-upgrades ]; then
    sudo sed -i 's|^//Unattended-Upgrade::Automatic-Reboot "false";|Unattended-Upgrade::Automatic-Reboot "false";|' /etc/apt/apt.conf.d/50unattended-upgrades
fi
echo "    sleep targets masked, cron $(systemctl is-active cron), ufw $(sudo ufw status | head -1 | awk '{print $2}')"

# ------------------------------------------------------------ 4. gcloud CLI
step "gcloud CLI"
if ! command -v gcloud >/dev/null 2>&1; then
    curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo gpg --dearmor --yes -o /usr/share/keyrings/cloud.google.gpg
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | sudo tee /etc/apt/sources.list.d/google-cloud-sdk.list >/dev/null
    sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq google-cloud-cli >/dev/null
fi
echo "    $(gcloud --version 2>/dev/null | head -1)"

# ------------------------------------------------------------ 5. venv
step "Python venv + requirements"
if [ ! -x venv/bin/python ]; then python3 -m venv venv; fi
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt
. scripts/node_env.sh
echo "    $(node_env_describe)"

# ------------------------------------------------------------ 6. .env
step ".env — private, and free of the Dhan account-control keys (decision #46)"
if [ -f .env ]; then
    sed -i '/^DHAN_PIN=/d;/^DHAN_TOTP_SECRET=/d;/^DHAN_API_KEY=/d;/^DHAN_API_SECRET=/d' .env
    chmod 600 .env
    echo "    stripped; $(grep -c . .env) lines remain"
else
    echo "    !! no .env — copy it from the Mac (bootstrap_node_from_mac.sh does this)"
fi
rm -f data/.mac_auto_sync_state 2>/dev/null   # the Mac's throttle stamp must not delay the node's first run
mkdir -p logs

# ------------------------------------------------------------ 7. Ollama
if [ "$WITH_OLLAMA" -eq 1 ]; then
    step "Ollama (on-demand per job; service disabled at boot)"
    command -v ollama >/dev/null 2>&1 || curl -fsSL https://ollama.com/install.sh | sh
    model="$(grep -E '^OLLAMA_MODEL=' .env 2>/dev/null | cut -d= -f2-)"; model="${model:-llama3}"
    sudo systemctl start ollama >/dev/null 2>&1 || true          # the pull needs a server for a minute
    ollama pull "$model" || echo "    !! pull failed — run: sudo systemctl start ollama && ollama pull $model"
    sudo systemctl disable --now ollama >/dev/null 2>&1 || true  # the scripts start/stop it per job (ledger Issue 23)
fi

# ------------------------------------------------------------ 8. gcloud auth
step "gcloud login (interactive: it prints a command to run on the Mac; paste the result back)"
if [ -z "$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null)" ]; then
    gcloud auth login --no-browser || echo "    !! login not completed — run: gcloud auth login --no-browser"
fi
gcloud config set project project-37632031-10d0-47dd-b6f >/dev/null 2>&1
gcloud config set compute/zone us-central1-a >/dev/null 2>&1
echo "    account: $(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null || echo none)"
step "First contact with the VM (registers this box's key; read-only)"
timeout 120 gcloud compute ssh adigupta1998@alpha-trading-vm --project=project-37632031-10d0-47dd-b6f --zone=us-central1-a --quiet --command='echo "    VM says: $(hostname)"' \
    || echo "    !! VM not reachable yet — the preflight below will say why"

# ------------------------------------------------------------ 9. preflight
step "Preflight (read-only)"
if bash scripts/node_preflight.sh; then
    if [ "$NO_INSTALL" -eq 1 ]; then
        echo; echo "READY — stopping before the cron install (--no-install)."; exit 0
    fi
    step "Installing the SHADOW schedule and running one forced sync"
    bash scripts/setup_mininode_cron.sh --shadow
    ALPHA_NODE_SHADOW=1 bash scripts/mac_auto_sync.sh --force | tail -8
    echo
    echo "DONE. The node is in its shadow week; the Mac keeps its agents. In seven days:"
    echo "    cd ~/alpha_trading && python3 scripts/node_trial_report.py"
    exit 0
else
    echo; echo "NOT READY — fix the FAIL lines above (each names its command), then re-run: bash scripts/bootstrap_node.sh"
    exit 1
fi
