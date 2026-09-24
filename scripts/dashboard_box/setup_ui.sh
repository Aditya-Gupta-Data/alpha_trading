#!/bin/bash
# setup_ui.sh — deploy the React desk + read-only API bridge on the dashboard
# box (Oracle Linux 9), behind nginx on :8080, which the existing Cloudflare
# quick tunnel then publishes (decision #113). Idempotent; re-run to update.
# Assumes scripts/dashboard_box/setup.sh already ran (venv, /etc/alpha-dashboard.env,
# alpha-dashboard + cloudflared-dashboard services).
set -euo pipefail
APP="/opt/alpha_trading"
ENVF="/etc/alpha-dashboard.env"
cd "$APP" && git pull -q --ff-only
# 1. runtimes: Node 22 (AppStream module) + nginx; SELinux lets nginx dial local ports.
#    NO npm on this box: the desk bundle is BUILT ON THE MAC and shipped by
#    scripts/dashboard_box/ship_ui.sh (a vite build thrashes a 1 GB box — seen twice).
sudo dnf -q -y module enable nodejs:22 >/dev/null 2>&1 || true
sudo dnf -q -y --setopt=install_weak_deps=False install nodejs nginx >/dev/null
sudo setsebool -P httpd_can_network_connect 1
# 2. python side: the bridge shares the showcase venv
# python -m pip: the venv was created under /home and moved to /opt, so venv/bin/pip has a stale shebang
venv/bin/python -m pip install -q --no-cache-dir -r requirements-dashboard.txt
# 3. the shipped bundle must already be here
if [ ! -f frontend/.output/server/index.mjs ]; then
  echo "frontend/.output missing — run  bash scripts/dashboard_box/ship_ui.sh  from the Mac first"; exit 1
fi
sudo restorecon -R "$APP/frontend" >/dev/null 2>&1 || true
# 4. services
sudo tee /etc/systemd/system/alpha-api-bridge.service >/dev/null <<UNIT
[Unit]
Description=Alpha Desk read-only API bridge (FastAPI, GET only)
After=network-online.target
[Service]
User=$USER
WorkingDirectory=$APP
EnvironmentFile=$ENVF
ExecStart=$APP/venv/bin/python -m uvicorn src.dashboard.api_bridge:app --host 127.0.0.1 --port 8600
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
sudo tee /etc/systemd/system/alpha-desk-ui.service >/dev/null <<UNIT
[Unit]
Description=Alpha Desk React UI (TanStack Start SSR)
After=network-online.target alpha-api-bridge.service
[Service]
User=$USER
WorkingDirectory=$APP/frontend
Environment=PORT=3000
Environment=HOST=127.0.0.1
Environment=NODE_ENV=production
ExecStart=/usr/bin/node .output/server/index.mjs
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
# Streamlit moves under /streamlit/ so nginx can host both on one origin
sudo sed -i 's#--server.fileWatcherType none#--server.fileWatcherType none --server.baseUrlPath streamlit#' /etc/systemd/system/alpha-dashboard.service
sudo sed -i 's#--server.baseUrlPath streamlit --server.baseUrlPath streamlit#--server.baseUrlPath streamlit#' /etc/systemd/system/alpha-dashboard.service
# nginx front door + tunnel repointed at it
sudo cp "$APP/scripts/dashboard_box/nginx-desk.conf" /etc/nginx/conf.d/desk.conf
sudo nginx -t
sudo sed -i 's#--url http://127.0.0.1:8501#--url http://127.0.0.1:8080#' /etc/systemd/system/cloudflared-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable --now nginx alpha-api-bridge alpha-desk-ui
sudo systemctl restart alpha-dashboard nginx alpha-api-bridge alpha-desk-ui cloudflared-dashboard
sleep 10
for s in alpha-api-bridge alpha-desk-ui alpha-dashboard nginx cloudflared-dashboard; do printf '%-22s %s\n' "$s" "$(systemctl is-active $s)"; done
curl -s -o /dev/null -w 'bridge  /api/health   %{http_code}\n' http://127.0.0.1:8600/api/health
curl -s -o /dev/null -w 'nginx   /             %{http_code}\n' http://127.0.0.1:8080/
curl -s -o /dev/null -w 'nginx   /api/health   %{http_code}\n' http://127.0.0.1:8080/api/health
echo "public URL: $(sudo journalctl -u cloudflared-dashboard --no-pager | grep -o 'https://[a-z0-9.-]*trycloudflare.com' | tail -1)"
