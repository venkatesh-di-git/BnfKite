#!/usr/bin/env bash
# sync.sh — push source to the VM. Run from Git Bash in the project root.
#
#   ./deploy/sync.sh user@vm-host
#   KITE_VM=user@vm-host ./deploy/sync.sh
#
# Deliberately does NOT restart the service. You test here, approve, then apply
# when you choose — a mid-session restart costs ~60s of no alerts while
# _ema_history refills (measured 76s on 06 Aug).
set -euo pipefail

# On GCE, SSH-in-browser cannot scp — you need a real client. Auth is an
# ed25519 key in instance metadata; put the connection details in a
# ~/.ssh/config Host block and pass that alias as $VM (see vm_handover.md §2).
# The external IP is ephemeral, so a stop/start means editing that one entry.
VM="${1:-${KITE_VM:?usage: sync.sh user@host   (or set KITE_VM)}}"
DEST="kite-scanner"

cd "$(dirname "$0")/.."

ssh "$VM" "mkdir -p ~/$DEST"

# scp rather than rsync for two reasons.
#
# Git Bash ships ssh and scp but NOT rsync, so an rsync script would fail on the
# one machine it is meant to run from.
#
# And the glob is safe by construction: .env, .kite_session_cache.json, csv/,
# __pycache__ and venv/.venv do not match *.py, so this cannot repeat the leak the
# manual zips kept causing. The narrowness IS the safety property — widening it
# to `scp -r .` would undo that.
#
# Consequences worth knowing: deploy/ and Markdowns/ are not synced (copy the
# unit file by hand when it changes), and a file deleted here lingers there.
scp *.py requirements.txt "$VM:$DEST/"

# replay/ is a package, so it needs its own line — the glob above is top-level
# only. Still an explicit whitelist rather than `scp -r .`, which is what keeps
# .env, csv/ and archive/ from ever travelling. Note archive/ deliberately does
# NOT sync in either direction: it is generated on the VM (which holds the token)
# and pulled down with pull-archive.sh, so pushing it would risk overwriting the
# durable copy with a partial one.
ssh "$VM" "mkdir -p ~/$DEST/replay"
scp replay/*.py "$VM:$DEST/replay/"

# The frozen golden files. Data, not code, so *.py misses them — and without them
# the golden tests fail on the VM for a reason that looks like engine drift but is
# a missing fixture. They are small (one row per alert) and must be byte-identical
# on both machines, since the whole point is detecting when output changes.
ssh "$VM" "mkdir -p ~/$DEST/replay/golden"
scp replay/golden/*.csv "$VM:$DEST/replay/golden/"

# NOT synced, deliberately: vm-csv/. It is a pulled COPY of the VM's own csv/, so
# pushing it back would have the VM comparing against a stale snapshot of itself.
# replay.compare and replay.cooldown_study resolve vm-csv/ on the workstation and
# csv/ on the VM, which is why the same tests pass on both without it.

echo
echo "Synced to $VM:~/$DEST"
echo "Apply when ready:  ssh $VM 'systemctl --user restart kite-scanner'"
