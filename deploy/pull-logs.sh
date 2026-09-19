#!/usr/bin/env bash
# pull-logs.sh — fetch the VM's CSV logs here for tuning. Run from Git Bash.
#
#   ./deploy/pull-logs.sh user@vm-host
#   ./deploy/pull-logs.sh --check user@vm-host     # report drift, fetch nothing
#   KITE_VM=user@vm-host ./deploy/pull-logs.sh
#
# Run it after the close, once the session's rows are written.
#
# WHY --check EXISTS. vm-csv/ is a SNAPSHOT, and nothing about it announces its
# own age. Every published figure in replay_engine_spec_v3.md was derived from it,
# so a stale copy does not produce an error — it produces confident numbers about
# a corpus that has since moved on. --check answers "is what I am about to analyse
# still what the VM has" without transferring anything.
set -euo pipefail

CHECK=0
if [ "${1:-}" = "--check" ]; then
    CHECK=1
    shift
fi

# See sync.sh: pass your ~/.ssh/config Host alias as $VM.
VM="${1:-${KITE_VM:?usage: pull-logs.sh [--check] user@host   (or set KITE_VM)}}"

cd "$(dirname "$0")/.."

LOCAL_LOG="vm-csv/alert_log.csv"
REMOTE_LOG="kite-scanner/csv/alert_log.csv"

# Field 2 is the timestamp. Safe with cut despite reason_list possibly containing
# commas: field 1 is a uuid, so nothing before the timestamp can be quoted.
summarise_local() {
    if [ ! -f "$LOCAL_LOG" ]; then
        echo "0 rows | (no local copy yet)"
        return
    fi
    local rows last
    rows=$(( $(wc -l < "$LOCAL_LOG") - 1 ))
    last=$(tail -n +2 "$LOCAL_LOG" | cut -d, -f2 | tail -1 | cut -c1-10)
    echo "$rows rows | latest $last"
}

summarise_remote() {
    ssh "$VM" "rows=\$(( \$(wc -l < $REMOTE_LOG) - 1 )); \
               last=\$(tail -n +2 $REMOTE_LOG | cut -d, -f2 | tail -1 | cut -c1-10); \
               echo \"\$rows rows | latest \$last\""
}

BEFORE=$(summarise_local)
REMOTE=$(summarise_remote)

echo "  local  vm-csv/ : $BEFORE"
echo "  remote VM csv/ : $REMOTE"

if [ "$CHECK" = "1" ]; then
    echo
    if [ "$BEFORE" = "$REMOTE" ]; then
        echo "IN SYNC — vm-csv/ matches the VM."
        exit 0
    fi
    echo "STALE — the VM has moved on. Re-pull with:  ./deploy/pull-logs.sh $VM"
    exit 1
fi

# vm-csv/, not csv/. The local csv/ holds whatever a local app.py run produced;
# dropping VM files on top would overwrite some and interleave others, splitting
# a single session's history across two engines. That is precisely the corpus
# the replay engine exists to compare against, so it has to stay clean.
mkdir -p vm-csv
scp "$VM:kite-scanner/csv/*" vm-csv/

echo
echo "  pulled         : $(summarise_local)"
echo
echo "VM logs in vm-csv/ — kept separate from local csv/ on purpose."

# The published figures do NOT move on their own, and that is deliberate: the
# corpus tests pin replay/cooldown_study.py's CORPUS_START..CORPUS_END so a new
# session cannot silently redefine "56 alerts, 18 flips" underneath the spec.
# Widening the window is therefore an explicit act, and the spec's numbers must be
# regenerated in the same commit that widens it.
echo
echo "Published figures stay pinned to CORPUS_START..CORPUS_END in"
echo "replay/cooldown_study.py. To include newly pulled sessions:"
echo
echo "  BN_DRY_RUN=1 python -m replay.cooldown_study --from 2026-08-11 --to <new-end>"
echo
echo "then update the window and the figures in Markdowns/replay_engine_spec_v3.md §15."
