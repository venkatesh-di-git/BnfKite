#!/usr/bin/env bash
# pull-archive.sh — bring the candle archive down from the VM. Run from Git Bash.
#
#   ./deploy/pull-archive.sh user@vm-host
#   KITE_VM=user@vm-host ./deploy/pull-archive.sh
#
# The archive is generated on the VM because that is where the live Kite token
# lives, but the VM must never be the only copy: it is a trial-tier e2-micro on
# an ephemeral IP, and once a contract settles its candles cannot be re-fetched
# from anywhere. Run this after every backfill.
set -euo pipefail

VM="${1:-${KITE_VM:?usage: pull-archive.sh user@host   (or set KITE_VM)}}"

cd "$(dirname "$0")/.."
mkdir -p archive

# -r because the archive is one directory per tradingsymbol. Safe to re-run:
# files are written once and never rewritten, so this only ever adds.
scp -r "$VM:kite-scanner/archive/"* archive/

echo
for d in archive/*/; do
    [ -d "$d" ] || continue
    sym=$(basename "$d")
    printf '%-22s %3s sessions  %s\n' "$sym" \
        "$(ls "$d" | grep -c '\.json\.gz$' || true)" \
        "$(du -sh "$d" | cut -f1)"
done
echo
echo "Archive is now on both machines. Neither copy is disposable."
