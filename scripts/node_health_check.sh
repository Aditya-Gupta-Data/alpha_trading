#!/bin/bash
# scripts/node_health_check.sh — is the Mini PC working? (READ-ONLY; run it FROM THE MAC)
# ==============================================================================
# MANUAL OFFLINE TOOL (2026-10-11). Nothing schedules this. From the Mac's repo checkout:
#
#     ssh minipc1 'bash -s' < scripts/node_health_check.sh
#
# It streams over ssh, so the node needs no copy of this file. It prints one section per
# concern (clock, network and whether it is on the cable, Tailscale, SSH, never-sleep,
# disk / memory / temperature, reboot and power history, the repo, the venv, the .env KEY
# NAMES with values hidden, the data seed, cron blocks, heartbeat, recent logs, reconcile
# reports, gcloud, Ollama, firewall, and the node preflight). It changes nothing, installs
# nothing, never fetches, and never prints a secret.
# READ-ONLY Sunday-night health check for the Mini PC. Changes nothing, prints no secrets.
cd ~/alpha_trading 2>/dev/null || echo "!! no ~/alpha_trading on this machine"
h() { printf '\n== %s ==\n' "$1"; }

h "WHO / WHEN";        hostname; whoami; date '+%F %T %Z (%z)'; uptime -p; echo "up since: $(uptime -s)"
h "CLOCK";             timedatectl 2>&1 | grep -E 'Time zone|synchronized|NTP service'
h "NETWORK (en*/eth* = cable, wl* = Wi-Fi)"; ip -br -4 addr 2>&1 | grep -v '^lo'; ip route 2>&1 | head -3
h "INTERNET";          timeout 5 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443' 2>/dev/null && echo "internet: reachable" || echo "internet: NOT reachable"
h "TAILSCALE";         tailscale ip -4 2>&1; tailscale status 2>&1 | head -6
h "SSH";               echo "ssh active: $(systemctl is-active ssh 2>&1), enabled: $(systemctl is-enabled ssh 2>&1)"
h "NEVER SLEEP";       for t in sleep suspend hibernate hybrid-sleep; do echo "$t.target: $(systemctl is-enabled $t.target 2>&1)"; done; grep -E '^IdleAction' /etc/systemd/logind.conf 2>&1
h "RESOURCES";         df -h / | tail -1; free -h | sed -n 2p; for z in /sys/class/thermal/thermal_zone*/temp; do [ -r "$z" ] && echo "temp: $(( $(cat "$z") / 1000 )) C" && break; done
h "BOOT / POWER HISTORY"; last -x reboot shutdown 2>/dev/null | head -6; journalctl --list-boots --no-pager 2>/dev/null | tail -4
h "PENDING UPDATES";   apt list --upgradable 2>/dev/null | grep -vc '^Listing'
h "REPO";              git branch --show-current 2>&1; git log --oneline -1 2>&1; echo "modified tracked files: $(git status --short 2>/dev/null | grep -vc '^??')"
h "PYTHON";            if [ -x venv/bin/python ]; then venv/bin/python --version 2>&1; else echo "!! no venv at ~/alpha_trading/venv"; fi
h ".ENV KEY NAMES (values hidden)"; if [ -f .env ]; then sed -E 's/=.*/=<hidden>/' .env | grep -vE '^\s*(#|$)' | sort; echo "perms: $(stat -c %a .env)"; else echo ".env MISSING"; fi
h "DATA"; ls -la data/brain_map.db data/journal.jsonl data/.node_seed.json 2>&1 | awk '{print $5, $6, $7, $8, $9}'; du -sh data 2>&1; cat data/.node_seed.json 2>/dev/null | grep -E 'seeded_at|vm_sha'
h "CRON BLOCKS";       crontab -l 2>&1 | grep -E 'BLOCK (START|END)|^ALPHA_NODE_|^@reboot|^OPS_EXPECTED' | cut -c1-110; echo "alpha_trading cron lines: $(crontab -l 2>/dev/null | grep -c alpha_trading)"
h "HEARTBEAT";         tail -2 logs/node_heartbeat.log 2>&1; echo "beats today: $(grep -c "|$(date +%Y-%m-%d)" logs/node_heartbeat.log 2>/dev/null)"
h "RECENT LOGS";       ls -lt logs 2>/dev/null | head -8 | awk '{print $6, $7, $8, $9}'; echo "-- mac_auto_sync tail:"; tail -4 logs/mac_auto_sync.log 2>&1
h "RECONCILE REPORTS"; ls -1t logs/node_reconcile 2>/dev/null | head -4
h "GCLOUD";            gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>&1; gcloud config get-value project 2>&1
h "OLLAMA (optional)"; command -v ollama >/dev/null && echo "installed; service enabled: $(systemctl is-enabled ollama 2>&1)" || echo "not installed"
h "FIREWALL";          sudo -n ufw status 2>&1 | head -4
h "PREFLIGHT (read-only, VM round-trip skipped)"; [ -f scripts/node_preflight.sh ] && bash scripts/node_preflight.sh --no-vm 2>&1 | tail -70 || echo "scripts/node_preflight.sh not on this checkout (old code)"
