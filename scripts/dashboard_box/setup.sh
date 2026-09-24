#!/bin/bash
# setup.sh — ONE-TIME setup of the always-on dashboard box (decision #112),
# written for the Oracle Cloud always-free VM (Oracle Linux 9, 1 OCPU, ~0.5-1 GB).
# Run as the login user (opc). Installs Python 3.11 + Streamlit in a venv, clones
# the repo, generates the access key IN PLACE (never pasted), opens :8501 in
# firewalld, and installs a systemd service that restarts on failure/boot.
# The trading VM PUSHES the ledger mirror here (scripts/publish_dashboard_mirror.sh);
# this box only serves.
set -euo pipefail
REPO="${REPO:-https://github.com/Aditya-Gupta-Data/alpha_trading}"
APP="/opt/alpha_trading"     # NOT $HOME: SELinux (Enforcing on Oracle Linux) refuses systemd exec/env reads from /home
# swap: Streamlit + pandas on a small box
if ! swapon --show | grep -q swapfile; then
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi
# dnf AFTER swap, on purpose: on a 498 MB box dnf's metadata refresh alone
# can take 300-400 MB and thrash the machine to a halt (2026-09-24, seen live).
sudo dnf -q -y --setopt=install_weak_deps=False install python3.11 python3.11-pip git rsync >/dev/null
[ -d "$APP" ] || { sudo git clone -q "$REPO" "$APP" && sudo chown -R "$USER:$USER" "$APP"; }
cd "$APP" && git pull -q --ff-only
[ -d venv ] || python3.11 -m venv venv
venv/bin/pip install -q --no-cache-dir --upgrade pip && venv/bin/pip install -q --no-cache-dir -r requirements-dashboard.txt
mkdir -p data/vm_mirror
ENVF="/etc/alpha-dashboard.env"   # root-owned, in /etc — systemd cannot read an EnvironmentFile under /home with SELinux enforcing
if [ ! -f "$ENVF" ]; then
  KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
  printf 'DASHBOARD_KEY=%s\nALPHA_DATA_DIR=%s/data/vm_mirror\n' "$KEY" "$APP" | sudo tee "$ENVF" >/dev/null; sudo chmod 600 "$ENVF"; sudo restorecon "$ENVF"
  echo "ACCESS KEY (save it now, shown once): $KEY"
fi
sudo firewall-cmd -q --permanent --add-port=8501/tcp && sudo firewall-cmd -q --reload
( crontab -l 2>/dev/null | grep -v 'dashboard_box' ; \
  echo "5 3 * * * cd $APP && git pull -q --ff-only && venv/bin/pip install -q -r requirements-dashboard.txt && sudo systemctl restart alpha-dashboard # dashboard_box nightly code refresh" ) | crontab -
sudo tee /etc/systemd/system/alpha-dashboard.service >/dev/null <<UNIT
[Unit]
Description=Alpha Desk showcase dashboard (read-only Streamlit)
After=network-online.target
[Service]
User=$USER
WorkingDirectory=$APP
EnvironmentFile=$ENVF
ExecStart=$APP/venv/bin/python -m streamlit run src/dashboard/app.py --server.port 8501 --server.address 0.0.0.0 --server.headless true --browser.gatherUsageStats false --server.runOnSave false --server.fileWatcherType none
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
sudo restorecon -R "$APP"
sudo systemctl daemon-reload && sudo systemctl enable --now alpha-dashboard
# Cloudflare quick tunnel (decision #112 addendum): OCI's security list / NSG
# kept dropping inbound 8501 on 2026-09-24, so the box publishes itself
# outbound instead — no inbound port needed, HTTPS, key-gated by the app.
# The trycloudflare URL rotates when this service restarts (boot); read it:
#   sudo journalctl -u cloudflared-dashboard --no-pager | grep -o 'https://[a-z0-9.-]*trycloudflare.com' | tail -1
if ! command -v cloudflared >/dev/null; then
  curl -fsSL -o /tmp/cf.rpm https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-x86_64.rpm
  sudo rpm -i /tmp/cf.rpm >/dev/null 2>&1 || sudo rpm -U /tmp/cf.rpm
fi
sudo tee /etc/systemd/system/cloudflared-dashboard.service >/dev/null <<UNIT
[Unit]
Description=Cloudflare quick tunnel -> alpha dashboard :8501
After=network-online.target alpha-dashboard.service
Requires=alpha-dashboard.service
[Service]
ExecStart=/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8501
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload && sudo systemctl enable --now cloudflared-dashboard
sleep 8 && systemctl is-active alpha-dashboard && echo "dashboard up on :8501 (open 8501/tcp in the OCI security list too)"
