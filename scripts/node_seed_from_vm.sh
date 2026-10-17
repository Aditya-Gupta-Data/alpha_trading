#!/bin/bash
# scripts/node_seed_from_vm.sh — start the replica from the VM's live state (run ON THE NODE)
# ==============================================================================
# Decision #144. A replica that begins from the Mac's July copy of the book would
# "diverge" from the VM on day one for no reason that says anything about the node.
# So, once, on a weekend: take a CONSISTENT snapshot of the VM's book (sqlite online
# backup + journal under its lock, via src.dashboard.mirror_snapshot — the same
# read-only door the edge miner uses), plus the VM's small state files, and make the
# node's data/ and logs/*.jsonl start identical.
#
# READ-ONLY ON THE VM. It writes only /tmp/node_seed on the VM, and removes it.
# It OVERWRITES the node's brain_map.db, journal.jsonl and the state files (backed up
# first to data/seed_backup_<time>/), so it demands --yes, refuses market hours, and
# refuses while the replica block is installed (jobs would be writing underneath it).
#
#   bash scripts/node_seed_from_vm.sh --yes [--allow-market-hours]
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
. scripts/node_env.sh

YES=0; HOURS_OK=0
for a in "$@"; do
    case "$a" in --yes) YES=1 ;; --allow-market-hours) HOURS_OK=1 ;; *) echo "usage: $0 --yes [--allow-market-hours]" >&2; exit 2 ;; esac
done
[ "$(uname -s)" = "Darwin" ] && { echo "node_seed: run this ON THE NODE." >&2; exit 1; }
case "$(hostname)" in alpha-trading-vm*) echo "node_seed: refusing to run on the VM." >&2; exit 1 ;; esac
[ "$YES" -eq 1 ] || { echo "node_seed: this OVERWRITES the node's book with the VM's. Re-run with --yes." >&2; exit 2; }
if crontab -l 2>/dev/null | grep -q 'NODE REPLICA BLOCK START'; then
    echo "node_seed: the replica block is installed — jobs would write under the seed. Run: bash scripts/setup_node_replica_cron.sh --remove, seed, then install again." >&2; exit 1
fi
DOW=$(date +%u); HM=$(date +%H%M)
if [ "$HOURS_OK" -eq 0 ] && [ "$DOW" -le 5 ] && [ "$HM" -ge 0850 ] && [ "$HM" -le 1600 ]; then
    echo "node_seed: it is market hours (Mon-Fri 08:50-16:00 IST) — the VM's book is moving. Seed in the evening or at the weekend, or pass --allow-market-hours." >&2; exit 1
fi
command -v gcloud >/dev/null 2>&1 || { echo "node_seed: gcloud not found." >&2; exit 1; }
GC=(gcloud compute ssh adigupta1998@alpha-trading-vm --project=project-37632031-10d0-47dd-b6f --zone=us-central1-a --quiet)
SCP=(gcloud compute scp --project=project-37632031-10d0-47dd-b6f --zone=us-central1-a --quiet --recurse)
step() { printf '\n==> %s\n' "$*"; }

step "Snapshotting the VM's book and state (read-only; writes only /tmp/node_seed there)"
"${GC[@]}" --command 'set -e; rm -rf /tmp/node_seed; mkdir -p /tmp/node_seed; cd ~/alpha_trading;
  venv/bin/python3 -m src.dashboard.mirror_snapshot /tmp/node_seed;
  { find data -maxdepth 1 -type f -size -50M ! -name "*.db" ! -name "*.db-*" ! -name "*.bak*" ! -name "*.lock" ! -name "*.part" ! -name "*.tmp" ! -name "journal.jsonl" ! -name ".mac_auto_sync_state" ! -name ".node_seed.json";
    find logs -maxdepth 1 -type f \( -name "*.jsonl" -o -name "*.json" \) -size -50M; } > /tmp/node_seed/files.txt;
  tar czf /tmp/node_seed/state.tgz -T /tmp/node_seed/files.txt;
  git rev-parse --short HEAD > /tmp/node_seed/sha.txt; ls -l /tmp/node_seed' \
  || { echo "node_seed: the VM snapshot failed — nothing on the node was changed." >&2; exit 1; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
step "Downloading"
"${SCP[@]}" adigupta1998@alpha-trading-vm:/tmp/node_seed "$TMP/" || { echo "node_seed: download failed — nothing changed." >&2; exit 1; }
SEED="$TMP/node_seed"
"${GC[@]}" --command 'rm -rf /tmp/node_seed' >/dev/null 2>&1 || true

step "Verifying before touching the node"
"$PY" - "$SEED" <<'PYEOF' || { echo "node_seed: the downloaded snapshot failed verification — nothing changed." >&2; exit 1; }
import sqlite3, sys, tarfile, pathlib
d = pathlib.Path(sys.argv[1])
db = sqlite3.connect(f"file:{d/'brain_map.db'}?mode=ro", uri=True)
ok = db.execute("PRAGMA integrity_check").fetchone()[0]
assert ok == "ok", f"integrity_check: {ok}"
n = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
assert n > 5, f"only {n} tables"
assert (d/"journal.jsonl").stat().st_size > 0, "empty journal"
with tarfile.open(d/"state.tgz") as t:
    names = t.getnames()
    bad = [x for x in names if x.startswith("/") or ".." in x.split("/") or not (x.startswith("data/") or x.startswith("logs/"))]
    assert not bad, f"unsafe tar paths: {bad[:3]}"
print(f"    brain_map.db integrity ok, {n} tables; journal {(d/'journal.jsonl').stat().st_size} bytes; {len(names)} state files")
PYEOF

step "Installing on the node (existing copies backed up first)"
BK="data/seed_backup_$(date +%Y%m%d-%H%M%S)"; mkdir -p "$BK" data logs
for f in data/brain_map.db data/journal.jsonl data/portfolio.json data/human_pulse.json; do [ -f "$f" ] && cp -p "$f" "$BK/"; done
rm -f data/brain_map.db-journal data/brain_map.db-wal data/brain_map.db-shm
cp "$SEED/brain_map.db" data/brain_map.db && cp "$SEED/journal.jsonl" data/journal.jsonl
tar xzf "$SEED/state.tgz" -C .
VMSHA="$(cat "$SEED/sha.txt" 2>/dev/null || echo unknown)"
"$PY" - "$VMSHA" <<'PYEOF'
import hashlib, json, sys, datetime, pathlib
db = pathlib.Path("data/brain_map.db").read_bytes()
pathlib.Path("data/.node_seed.json").write_text(json.dumps({
    "seeded_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    "vm_sha": sys.argv[1], "db_sha256": hashlib.sha256(db).hexdigest(),
    "note": "node book seeded from the VM; reconcile differences are measured from here"}, indent=1))
PYEOF
echo "    seeded from VM @ $VMSHA; backups in $BK"
echo; echo "DONE. Next: bash scripts/setup_node_replica_cron.sh   (add --with-token only when you mean to give the node a live Dhan token)"
