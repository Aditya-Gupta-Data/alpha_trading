# HOME_NODE_SETUP.md — the Mini PC as an always-on Ubuntu Server

*Written 2026-10-10 for decisions #99 and #143. Plain English, copy-paste
commands, in the order you do them. `CRON_SETUP.md` ("The home node") is the
schedule; this file is how the box gets to the point where that schedule can
be installed.*

---

## The one-command way (added 2026-10-10, late)

Once Ubuntu Server is installed with SSH on (§1–§2), everything from §3 to
§10 is one command **on the Mac**, which can reach the box where a cloud
session cannot:

```bash
cd /Users/adityagupta/Documents/Claude/alpha_trading
ssh-copy-id mini_pc1@192.168.29.158                                      # once; after this no password prompts
bash scripts/bootstrap_node_from_mac.sh mini_pc1@192.168.29.158          # add --with-ollama for the LLM jobs
```

**As found on 2026-10-10:** the box is `minipc1`, user `mini_pc1`, Ubuntu
Server **26.04.1** (fine — newer than the 24.04 below; the preflight checks
the python it ships), LAN address `192.168.29.158`, Tailscale address
`100.72.160.38`. The Tailscale address answers only from a machine on the
same tailnet; the Mac was not (its `tailscale status` showed one node), so
use the LAN address. The box was on **Wi-Fi** (`wlx00e9…`, a USB dongle) —
move it to the cable before the trial week: Wi-Fi drops are exactly the
failure the always-on box exists to end. After plugging in, `ip -4 addr`
should show a second address on an `en…`/`eth…` interface; the DHCP
reservation goes on that interface's MAC, and the LAN address may change.

It copies this checkout, `data/` and `.env` to the node (nothing deleted
there, never `logs/`), then runs `scripts/bootstrap_node.sh` on the node over
`ssh -t`. You will be asked for two things on the way: the node's sudo
password, and the gcloud login (it prints a command to run in a second Mac
terminal; paste the result back). It ends with the read-only preflight and,
on READY, installs the SHADOW schedule and does one forced sync. Re-running
it after a fix is safe. The manual sections below are the same steps, for
when you want to see what it did.

## 0. What this box is, and what it is not

**It is the home lane.** It does exactly the jobs the Mac used to do from its
LaunchAgents and crontab: the NSE and Yahoo fetches that need a home IP, the
valuation pass that needs the corpus, the 7-file ship to the VM, the two
local-Ollama jobs, the Saturday scrip check. Those jobs are off the trading
path. If the box is dark for a day, nothing trades differently.

**It is NOT a second trading engine.** "Another VM" here means another
always-on Linux box that the tool runs on, not a copy of the GCP VM. Never:

- run `scripts/setup_cron.sh` on it (that is the VM's schedule),
- install `alpha-trading.service` / `alpha-discord-bot` on it,
- renew or push a Dhan token from it (decision #48 — one active token, the
  VM owns it),
- give it the Dhan PIN / TOTP secret / API key / API secret (decision #46 —
  those are account-control credentials; the node needs none of them).

The installer `scripts/setup_mininode_cron.sh` refuses to run on the VM and
on macOS, and never touches a token. That is by design.

**Hardware on record (decision #99):** Dell Mini PC, i3, 8 GB RAM, 256 GB
disk. 8 GB keeps Ollama at the same ceiling as the Mac; nothing else here
needs more than a few hundred MB.

---

## 1. BIOS — before the OS

Press the BIOS key at power-on (F2 on a Dell) and set:

| Setting | Value | Why |
|---|---|---|
| Restore on AC Power Loss (Power Management → AC Recovery) | **Power On** | The UPS only delays an outage. When its battery dies the box goes hard off, and this is what brings it back when mains returns. |
| Auto Power Off / Deep Sleep / ErP | **Off / Disabled** | Any timer that powers the box down kills cron. |
| Wake on LAN | On (optional) | Lets you wake it from the Mac if it ever does go down. |
| Boot order | the Ubuntu disk first | So an unattended reboot never waits at a menu. |

---

## 2. Install Ubuntu Server

- **Ubuntu Server 24.04 LTS** (it ships Python 3.12, which `requirements.txt`
  needs; 22.04 ships 3.10 and will not work without extra steps).
- Use the whole disk, default (non-LVM is fine), **no** full-disk encryption
  (an encrypted disk asks for a passphrase at every boot and an unattended
  reboot would sit there forever).
- Username: anything; this guide uses `alpha`. Hostname: `alpha-node`
  (anything but `alpha-trading-vm…`, which the installer refuses).
- Tick **"Install OpenSSH server"** so you can do the rest from the Mac.
- Skip every snap on the "Featured Server Snaps" screen.
- Plug the LAN cable in before the installer runs so it picks up DHCP.

When it reboots, log in once on the box, note its IP (`ip -4 addr show`) and
do everything else over SSH from the Mac:

```bash
ssh alpha@<node-ip>
```

---

## 3. First boot — make it an appliance

```bash
# clock: IST and kept right by NTP (cron fires on wall-clock time)
sudo timedatectl set-timezone Asia/Kolkata
sudo timedatectl set-ntp true
date +%z          # must print +0530 — the installer refuses anything else

# the box must never sleep or suspend
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
sudo sed -i 's/^#\?IdleAction=.*/IdleAction=ignore/' /etc/systemd/logind.conf
sudo systemctl restart systemd-logind

# packages the tool needs
sudo apt update && sudo apt install -y git python3 python3-venv python3-pip cron openssh-server rsync curl ca-certificates ufw

# cron is the scheduler — make sure it is on
sudo systemctl enable --now cron

# firewall: nothing inbound except SSH on the LAN
sudo ufw allow OpenSSH && sudo ufw --force enable

# security updates stay on, but the box must never reboot itself at a random hour
sudo apt install -y unattended-upgrades
sudo sed -i 's|^//Unattended-Upgrade::Automatic-Reboot "false";|Unattended-Upgrade::Automatic-Reboot "false";|' /etc/apt/apt.conf.d/50unattended-upgrades
```

**Give it a fixed address.** In your router's admin page, add a DHCP
reservation for the node's MAC address so `<node-ip>` never changes. That is
simpler and safer than a static IP on the box itself.

**Put the router and the fibre box on the same UPS.** A node that is up with
no internet fails every fetch and only logs it.

---

## 4. The repo

The repo is private, so the node needs its own GitHub key:

```bash
ssh-keygen -t ed25519 -C "alpha-node" -N "" -f ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub     # paste this at github.com → Settings → SSH and GPG keys → New SSH key
git clone git@github.com:Aditya-Gupta-Data/alpha_trading.git ~/alpha_trading
cd ~/alpha_trading
```

**Until the shadow-trial branch is merged to `main`**, check it out on the node, otherwise the installer has no `--shadow` and the preflight will say so:

```bash
git fetch origin claude/adoring-rubin-do2j66 && git checkout claude/adoring-rubin-do2j66
```

(After the merge: `git checkout main && git pull`.)

The cron block records the absolute path at install time, so keep the repo at
`~/alpha_trading` and do not move it afterwards.

---

## 5. Python

```bash
cd ~/alpha_trading
python3 --version                      # 3.12.x on 24.04
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
. scripts/node_env.sh && node_env_describe
```

The last line must print `py=/home/alpha/alpha_trading/venv/bin/python (repo
venv)`. If it says `PATH fallback`, the venv is missing or not executable and
every cron job will run on the wrong interpreter — fix that before anything
else (this is the lesson of three separate incidents, `CRON_SETUP.md`).

---

## 6. The corpus and the secrets — copied from the Mac, never through chat

`data/` is gitignored on purpose. The valuation pass reads
`data/lake/financial_results/`, the F&O fetch fills `data/lake/fo_bhavcopy/`,
the darling queue is `data/darlings_queue.json`, the bars cache is
`data/bars_cache.json`. Without the Mac's copy the node scores **0 of 109**
darlings and writes an empty file (the 2026-08-11 finding). So, from the Mac,
over the LAN, once:

```bash
# on the Mac — size first, then copy (data/ can be a few GB; logs/ is optional)
cd /Users/adityagupta/Documents/Claude/alpha_trading
du -sh data logs
rsync -avh --progress data/ alpha@<node-ip>:~/alpha_trading/data/
rsync -avh --progress logs/ alpha@<node-ip>:~/alpha_trading/logs/      # optional, history only
```

Then the `.env`. Copy it, then **strip the Dhan keys** — the node's jobs use
none of them, and decision #46 says the PIN/TOTP/API pair must not live on a
lower-trust box:

```bash
# on the Mac
scp .env alpha@<node-ip>:~/alpha_trading/.env
# on the node
cd ~/alpha_trading && sed -i '/^DHAN_PIN=/d;/^DHAN_TOTP_SECRET=/d;/^DHAN_API_KEY=/d;/^DHAN_API_SECRET=/d' .env
chmod 600 .env
grep -c DHAN_PIN .env    # must print 0
```

Keys the node does use: `DISCORD_WEBHOOK_URL` (the Saturday scrip card —
silenced in shadow mode anyway), `OLLAMA_MODEL` (optional, see §8),
`GEMINI_API_KEY` only if a job you run by hand needs it. Everything else is
harmless to leave in.

---

## 7. gcloud — the one lane to the VM

The ship (`vm_push_file`), the read-only pull of the trade book, the edge
miner's snapshot pull and the bars-cache refresh all go through
`gcloud compute scp/ssh` to `adigupta1998@alpha-trading-vm` in
`us-central1-a`, project `project-37632031-10d0-47dd-b6f`.

```bash
# install the CLI from Google's apt repo
sudo apt install -y apt-transport-https gnupg
curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | sudo tee /etc/apt/sources.list.d/google-cloud-sdk.list
sudo apt update && sudo apt install -y google-cloud-cli

# log in WITHOUT a browser on the box: it prints a command to run on the Mac,
# the Mac's browser does the login, you paste the result back
gcloud auth login --no-browser
gcloud config set project project-37632031-10d0-47dd-b6f
gcloud config set compute/zone us-central1-a

# first contact: this generates ~/.ssh/google_compute_engine and registers it on the VM
gcloud compute ssh adigupta1998@alpha-trading-vm --zone=us-central1-a --command='hostname && tail -1 ~/alpha_trading/logs/master_scheduler.log'
```

Use the **same Google account** the Mac uses, so the VM's user is
`adigupta1998` (the scripts hardcode it; a different account would create a
different Linux user on the VM and the ship would land in the wrong home).

`scripts/node_env.sh` pins `CLOUDSDK_PYTHON` to the venv python so cron's
`gcloud` never picks an unsupported interpreter (the 07-21 → 08-05 silent
ship failure).

---

## 8. Ollama — optional, for the two LLM jobs

The edge miner (21:00) and evolution (Saturday 02:00) use a local Ollama.
Both **fail open without it** (the miner reports "Ollama not running" and
skips), and the trial report does not need them to call the week RELIABLE.
Install it if you want those jobs on the node:

```bash
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl disable --now ollama      # the scripts start/stop it per job (ledger Issue 23); it must NOT run all day
ollama pull llama3                       # the default; ~4.7 GB on disk, tight-but-workable in 8 GB
```

If you see swapping during a 21:00 run (`free -h` shows swap in use), put
`OLLAMA_MODEL=llama3.2:3b` in `.env` and `ollama pull llama3.2:3b` — decision
#99 budgeted the box for 3B-class models.

---

## 9. Verify before scheduling anything

One command checks every step above and names the fix for anything missing:

```bash
cd ~/alpha_trading && bash scripts/node_preflight.sh
```

It is read-only. `READY` means every FAIL is gone (WARNs are advisory — Ollama, the bars cache, the F&O lake fill themselves on the first run). Then the longer checks:

```bash
cd ~/alpha_trading
venv/bin/python -m pytest -q                    # ~100 s; expect all green (one known checkout-dependent failure if data/portfolio.json is absent — HANDOVER 10-10)
. scripts/node_env.sh && node_env_describe      # repo venv, gcloud found
ALPHA_NODE_SHADOW=1 bash scripts/mac_auto_sync.sh --force
tail -20 logs/mac_auto_sync.cron.log 2>/dev/null; tail -20 logs/mac_auto_sync.log
```

What a healthy hand run looks like: `sector bars: ok`, `valuation: ok`,
`fo bhavcopy: ok`, `darling ids: ok`, `SHADOW: would have shipped 7/7`,
`pulled 1/1: docs/LIVE_TRADE_BOOK.md`. Each `FAILED` line names which fetch
died; the stored file is kept.

---

## 10. Install the shadow schedule and walk away

```bash
cd ~/alpha_trading && bash scripts/setup_mininode_cron.sh --shadow
crontab -l | grep -c alpha_trading       # a handful of lines, all inside the HOME NODE block
```

The Mac keeps its LaunchAgents all week. After seven full days:

```bash
cd ~/alpha_trading && git pull && python3 scripts/node_trial_report.py
```

`RELIABLE` → promote: `bash scripts/setup_mininode_cron.sh` (no flag), one
`bash scripts/mac_auto_sync.sh --force` that prints `shipped 7/7`, then the
Mac-retirement command in `CRON_SETUP.md`. `NOT YET` → it names the misses;
fix them, keep the Mac, run another week.

---

## 11. Keeping it healthy

| Symptom | Where to look | Usual cause |
|---|---|---|
| A slot never fired | `grep CRON /var/log/syslog`, `systemctl status cron` | box was off; cron not enabled; clock wrong |
| `sector bars: FAILED` | `logs/mac_auto_sync.log` | Yahoo throttling the home IP; retries next slot |
| `fo bhavcopy: FAILED` | `logs/fo_bhavcopy.log` | NSE 403 — the fetcher sets its own Referer; a VPN or datacentre IP would also do this |
| `valuation: FAILED` or scores 0 darlings | `data/lake/financial_results/` | the corpus was not copied from the Mac (§6) |
| `NOT pulled` / ship `NOT shipped` | `gcloud auth list` | gcloud login expired, or logged in as the wrong account |
| miner: `Ollama not running` | `logs/ollama_session.log` | Ollama not installed, or the model not pulled |
| box came back after a power cut but no catch-up run | `grep reboot logs/mac_auto_sync.cron.log` | the `@reboot` lines run 90 s / 120 s after boot; a cron block installed before 10-10 lacks them — re-run the installer |

Logs on the node are not rotated (the VM's `rotate_logs.sh` is cron #35
there, not here). `logs/mac_auto_sync.log` grows a few hundred KB a week;
truncate it whenever you like with `: > logs/mac_auto_sync.log`.

To update the code on the node: `cd ~/alpha_trading && git pull` — the cron
lines call the scripts by path, so a pull is all it takes. Re-run the
installer only when `CRON_SETUP.md` says the block changed.
