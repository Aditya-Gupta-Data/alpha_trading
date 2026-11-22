#!/bin/bash
# scripts/guest_web_terminal.sh — a login-protected WEB TERMINAL for the guest user (run ON the Mini PC, as root)
# ==============================================================================
# MANUAL OFFLINE TOOL (2026-11-22). Nothing schedules this; no cron line. Run after
# guest_user_setup.sh has made the guest user:
#
#     bash scripts/guest_web_terminal.sh [user]        # default user "hega"
#
# Installs `ttyd` and a systemd service that serves a browser terminal at
# http://<tailscale-ip>:7681 behind a username+password. The shell runs AS THE GUEST (never
# root, NoNewPrivileges), inside the guest's resource slice (same CPU/RAM caps as his SSH),
# bound to the Tailscale address only, with the firewall opened on tailscale0 only.
# Password lives in /etc/<user>-ttyd.env (root-only, mode 600). Re-run to change it.
# Refuses the VM, macOS, a missing guest user and a missing Tailscale address.
# ==============================================================================
G="${1:-hega}"
[ "$(id -u)" = "0" ] || { echo "run as root."; exit 1; }
[ "$(uname -s)" = "Linux" ] || { echo "Linux only."; exit 1; }
case "$(hostname)" in alpha-trading-vm*) echo "REFUSED: this is the VM."; exit 1 ;; esac
id "$G" >/dev/null 2>&1 || { echo "no user $G: run scripts/guest_user_setup.sh first."; exit 1; }
TSIP="$(tailscale ip -4 2>/dev/null | head -1)"
[ -n "$TSIP" ] || { echo "no Tailscale address: is Tailscale up on this machine?"; exit 1; }
PORT=7681; UIDG="$(id -u "$G")"

command -v ttyd >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq ttyd; }
command -v ttyd >/dev/null 2>&1 || { echo "ttyd could not be installed (apt). Send the error to the agent."; exit 1; }

read -rsp "Web login password for $G (no colon, 12+ characters): " P1; echo
read -rsp "Type it again: " P2; echo
[ "$P1" = "$P2" ] && [ "${#P1}" -ge 12 ] && [[ "$P1" != *:* ]] || { echo "passwords differ, too short, or contain ':'. Nothing changed."; exit 1; }
umask 077; printf 'CRED=%s:%s\n' "$G" "$P1" > "/etc/${G}-ttyd.env"; chmod 600 "/etc/${G}-ttyd.env"; umask 022

write_unit() {  # $1 = slice line (may be empty)
cat > "/etc/systemd/system/${G}-ttyd.service" <<UNIT
[Unit]
Description=Web terminal for guest $G (Tailscale only, not root)
After=network-online.target tailscaled.service
Wants=network-online.target
[Service]
User=$G
WorkingDirectory=/home/$G
EnvironmentFile=/etc/${G}-ttyd.env
ExecStart=$(command -v ttyd) -W -i $TSIP -p $PORT -c \${CRED} /bin/bash -l
NoNewPrivileges=yes
Restart=on-failure
RestartSec=10
$1
[Install]
WantedBy=multi-user.target
UNIT
}
write_unit "Slice=user-${UIDG}.slice"
systemctl daemon-reload && systemctl enable "${G}-ttyd.service" >/dev/null 2>&1; systemctl restart "${G}-ttyd.service"; sleep 2
if ! systemctl is-active --quiet "${G}-ttyd.service"; then
  echo "slice placement refused; using direct limits instead"
  write_unit "CPUWeight=20
CPUQuota=100%
MemoryHigh=40%
MemoryMax=50%
MemorySwapMax=0
IOWeight=20
TasksMax=300"
  systemctl daemon-reload; systemctl restart "${G}-ttyd.service"; sleep 2
fi
ufw allow in on tailscale0 to any port $PORT proto tcp >/dev/null 2>&1

echo; echo "=========== CHECK ==========="
echo "service : $(systemctl is-active "${G}-ttyd.service")"
echo "runs as : $(ps -o user= -C ttyd | head -1)  (want $G, not root)"
echo "listen  : $(ss -ltn | grep ":$PORT" | head -1)"
echo "slice   : $(systemctl show "${G}-ttyd.service" -p Slice --value)"
echo "URL     : http://$TSIP:$PORT   (login: $G + the password you just typed)"
