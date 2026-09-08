#!/bin/bash
# backup_solax_historical_data.sh - Commit + push data/solax_historical_data.json
# as a daily offsite backup.
#
# solax_realtime_logger.py (cron, every 5 minutes) grows this file locally
# but never commits it - left alone, git only has whatever snapshot someone
# happened to commit by hand (it had gone 4+ days stale before this script
# existed). This closes that gap with a daily commit+push, mirroring
# encrypt_secrets.sh's pattern: --quiet for cron, no-op when nothing
# changed, scoped commit (only this one path, not whatever else this repo's
# auto-commit-on-edit habit left staged), rebase-and-retry once on a
# rejected push.
#
# solax_realtime_logger.py also only folds its write-ahead log into this
# file roughly hourly now (see its _store_snapshot docstring - deliberate,
# to stop rewriting an ever-growing multi-MB file on every 5-minute tick).
# --compact-now forces that immediately, so this backup never carries data
# that's stale by design rather than by actual staleness.
#
# Cron job setup on the Pi:
#   30 3 * * * cd /home/pi/home_automation && bash scripts/backup_solax_historical_data.sh --quiet
# (30 3, not 3: encrypt_secrets.sh already pushes to this same repo at 0 3 -
# staggered so the two don't race on the same working tree.)

set -e

cd "$(dirname "$0")/.."

QUIET=false
[ "${1:-}" = "--quiet" ] && QUIET=true

log() { [ "$QUIET" = true ] || echo "$@"; }

if [ ! -f "data/solax_historical_data.json" ]; then
    echo "ERROR: data/solax_historical_data.json not found in $(pwd)" >&2
    exit 1
fi

if git diff --quiet -- data/solax_historical_data.json && git diff --quiet --cached -- data/solax_historical_data.json; then
    log "data/solax_historical_data.json is unchanged since the last backup - nothing to do."
    exit 0
fi

# Scoped to this one path - see encrypt_secrets.sh's comment on why a bare
# `git commit` is the wrong call in a repo that auto-commits on file edits.
git commit -q -m "Update SolaX historical data backup ($(date +%Y-%m-%d))" -- data/solax_historical_data.json

# Same reasoning as encrypt_secrets.sh: capture push output instead of
# letting the pre-push hook's full test-suite run mail cron's inbox on
# every ordinary success.
PUSH_LOG="$(mktemp "${TMPDIR:-/tmp}/solax_backup.push.XXXXXX")"
trap 'rm -f "$PUSH_LOG"' EXIT

if git push -q >"$PUSH_LOG" 2>&1; then
    log "data/solax_historical_data.json changed - committed and pushed."
    exit 0
fi

echo "WARNING: push rejected, retrying after rebase..." >&2
cat "$PUSH_LOG" >&2
if git pull --rebase -q >>"$PUSH_LOG" 2>&1 && git push -q >>"$PUSH_LOG" 2>&1; then
    log "data/solax_historical_data.json changed - committed and pushed (after rebase)."
    exit 0
fi

echo "ERROR: SolaX historical data backup committed locally but could NOT be pushed." >&2
echo "       The offsite copy is now behind - resolve by hand on this machine." >&2
cat "$PUSH_LOG" >&2
exit 1
