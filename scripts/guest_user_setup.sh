#!/bin/bash
# scripts/guest_user_setup.sh — give a guest a locked-down login on the Mini PC (run ON the node, once)
# ==============================================================================
# MANUAL OFFLINE TOOL (2026-11-22). Nothing schedules this; it is not a cron line and adds
# no job to any node block. Run it by hand on the Mini PC, as the owner's normal user:
#
#     bash scripts/guest_user_setup.sh              # guest user "hega"
#     bash scripts/guest_user_setup.sh friend       # another name
#
# What it makes: a separate Linux user with NO sudo/docker/gpu groups, a password login for
# that user only, a 30 GB fixed-size home (a loop file, so the real disk can never fill),
# CPU/RAM/IO/task caps with LOW priority (alpha trading wins under contention), and a block on
# the local Ollama port. The owner's home becomes mode 750 and .env 600, so the guest cannot
# read tokens or keys. It touches nothing in the repo, `data/`, `logs/` or the crontab.
# Logged in as root (web terminal)? Name the owner: OWNER_USER=<user> bash scripts/guest_user_setup.sh
# Safe to re-run. It refuses the VM and macOS (the VM is the only execution node, #146).
# ==============================================================================
G="${1:-hega}"
[[ "$G" =~ ^[a-z][a-z0-9_-]{1,30}$ ]] || { echo "bad user name: $G"; exit 1; }
[ "$(uname -s)" = "Linux" ] || { echo "Linux only."; exit 1; }
case "$(hostname)" in alpha-trading-vm*) echo "REFUSED: this is the VM."; exit 1 ;; esac
OWNER="$(id -un)"
# logged in as root (e.g. a web terminal)? name the owner's user: OWNER_USER=alpha bash g.sh
[ "$OWNER" = "root" ] && OWNER="${OWNER_USER:-${SUDO_USER:-}}"
[ -n "$OWNER" ] && [ "$OWNER" != "$G" ] && [ -d "/home/$OWNER" ] || { echo "who owns the repo? run: OWNER_USER=<that user> bash $0 $G   (see: ls /home)"; exit 1; }
IMG="/var/${G}.img"; SIZE_GB=30
sudo -v || exit 1

# 0. disk check
if [ ! -f "$IMG" ]; then
  FREE_GB=$(df --output=avail -BG /var | tail -1 | tr -dc 0-9)
  [ "$FREE_GB" -ge $((SIZE_GB+20)) ] || { echo "ABORT: ${FREE_GB}G free on /var, need $((SIZE_GB+20))G."; exit 1; }
fi

# 1. user, no privileged groups
id "$G" >/dev/null 2>&1 || sudo adduser --disabled-password --gecos "" "$G" || exit 1
for g in sudo docker video render adm; do sudo gpasswd -d "$G" "$g" >/dev/null 2>&1; done
echo "--- type the password for $G (twice) ---"; sudo passwd "$G"

# 2. fixed-size home
if ! mountpoint -q "/home/$G"; then
  [ -f "$IMG" ] || { sudo truncate -s ${SIZE_GB}G "$IMG" && sudo mkfs.ext4 -q "$IMG"; }
  grep -q "$IMG" /etc/fstab || echo "$IMG /home/$G ext4 loop 0 2" | sudo tee -a /etc/fstab >/dev/null
  sudo mount "/home/$G" && sudo cp -rT /etc/skel "/home/$G"
fi
sudo mkdir -p "/home/$G/app" "/home/$G/out"
sudo chown -R "$G:$G" "/home/$G"

# 3. the guest cannot read the owner's home
chmod 750 "/home/$OWNER"
[ -f "/home/$OWNER/alpha_trading/.env" ] && chmod 600 "/home/$OWNER/alpha_trading/.env"

# 4. compute caps, LOW priority
CORES=$(nproc); QUOTA=$(( CORES*100*3/8 )); [ "$QUOTA" -lt 100 ] && QUOTA=100
sudo systemctl set-property "user-$(id -u "$G").slice" \
  CPUWeight=20 CPUQuota=${QUOTA}% MemoryHigh=40% MemoryMax=50% MemorySwapMax=0 IOWeight=20 TasksMax=300

# 5. keep the guest off the local Ollama port, re-applied at boot
sudo tee "/etc/systemd/system/${G}-fw.service" >/dev/null <<UNIT
[Unit]
Description=Block $G from local Ollama
After=network.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'iptables -C OUTPUT -o lo -p tcp --dport 11434 -m owner --uid-owner $G -j REJECT 2>/dev/null || iptables -A OUTPUT -o lo -p tcp --dport 11434 -m owner --uid-owner $G -j REJECT'
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload && sudo systemctl enable --now "${G}-fw.service"

# 6. password login for this user only; jobs survive logout
sudo tee "/etc/ssh/sshd_config.d/50-${G}.conf" >/dev/null <<CONF
Match User $G
    PasswordAuthentication yes
    X11Forwarding no
CONF
sudo sshd -t && sudo systemctl reload ssh
sudo loginctl enable-linger "$G"

# 7. report
echo; echo "=========== CHECK ==========="
echo "Tailscale IP : $(tailscale ip -4 2>/dev/null | head -1)"
echo "$G groups    : $(id -nG "$G")"
echo "owner home   : $(stat -c %A "/home/$OWNER")  (want drwxr-x---)"
sudo -u "$G" ls "/home/$OWNER" >/dev/null 2>&1 && echo "FAIL: $G can read the owner's home" || echo "OK: $G cannot read the owner's home"
systemctl show "user-$(id -u "$G").slice" -p CPUQuotaPerSecUSec -p MemoryMax -p CPUWeight
df -h "/home/$G" | tail -1
