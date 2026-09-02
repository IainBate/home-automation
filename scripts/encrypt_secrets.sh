#!/bin/bash
# encrypt_secrets.sh - Encrypt secrets.yaml into secrets.yaml.enc for backup in git.
#
# secrets.yaml is gitignored (real credentials, never committed in plaintext),
# so if both this Mac and the Home Pi4 were lost, a fresh clone would have no
# way to recover it. secrets.yaml.enc is a deliberate exception: it IS meant
# to be committed.
#
# Passphrase: recommended is the Home Pi4's own login password - the cron
# job below runs there, so that's the password already at hand, and there's
# nothing new to remember or store. The script never uses it directly: it
# hashes whatever you type with SHA-256 first, and that hash is the actual
# openssl passphrase. See decrypt_secrets.sh for the recovery side.
#
# Passphrase source, checked in this order:
#   1. $SECRETS_BACKUP_PASSPHRASE, if set - for non-interactive first-time
#      setup (e.g. an agent following docs/PI4_DEPLOYMENT.md over SSH, which
#      has no terminal to prompt on).
#   2. secrets_backup.passphrase_hash in secrets.yaml, if set - a
#      pre-computed SHA-256 hash (this run prints one for you to save there).
#      Lets this run unattended, e.g. a daily cron job on the Home Pi4.
#   3. Otherwise, prompts interactively - twice, and they must match.
#
# --quiet: for cron. Requires option 1 or 2 above (fails rather than hanging
# on a prompt), and on success commits + pushes secrets.yaml.enc, but ONLY
# when secrets.yaml's own contents actually changed.
#
# Cron job setup on the Pi (needs push access on whatever SSH key `git`
# there already uses for the clone/pull in setup_pi.sh):
#   0 3 * * * cd /home/pi/home_automation && bash scripts/encrypt_secrets.sh --quiet

set -e

cd "$(dirname "$0")/.."
# shellcheck source=lib/secrets_common.sh
. "scripts/lib/secrets_common.sh"

QUIET=false
[ "${1:-}" = "--quiet" ] && QUIET=true

log() { [ "$QUIET" = true ] || echo "$@"; }

if [ ! -f "secrets.yaml" ]; then
    echo "ERROR: secrets.yaml not found in $(pwd)" >&2
    exit 1
fi

# --- Work files -------------------------------------------------------------
# mktemp, not a fixed name: a manual run and the 3am cron run overlapping on
# one shared "secrets.yaml.enc.tmp" corrupted the output in every trial. These
# hold decrypted plaintext, so they are created 0600 (mktemp's default) and
# removed on every exit path.
WORK_ENC="$(mktemp "${TMPDIR:-/tmp}/secrets.enc.XXXXXX")"
WORK_PLAIN="$(mktemp "${TMPDIR:-/tmp}/secrets.plain.XXXXXX")"
cleanup() { rm -f "$WORK_ENC" "$WORK_PLAIN"; }
trap cleanup EXIT

# --- Passphrase -------------------------------------------------------------
PASSPHRASE_HASH=""
if [ -n "${SECRETS_BACKUP_PASSPHRASE:-}" ]; then
    PASSPHRASE_HASH="$(printf '%s' "$SECRETS_BACKUP_PASSPHRASE" | sha256_hex)"
    log "Using passphrase from \$SECRETS_BACKUP_PASSPHRASE."
else
    PASSPHRASE_HASH="$(read_configured_passphrase_hash)"
fi

if [ -z "$PASSPHRASE_HASH" ]; then
    if [ "$QUIET" = true ]; then
        echo "ERROR: --quiet needs secrets_backup.passphrase_hash in secrets.yaml," >&2
        echo "       or \$SECRETS_BACKUP_PASSPHRASE in the environment." >&2
        exit 1
    fi
    if [ ! -t 0 ]; then
        echo "ERROR: no passphrase configured and no terminal to prompt on." >&2
        echo "       Set \$SECRETS_BACKUP_PASSPHRASE to run non-interactively." >&2
        exit 1
    fi
    echo "No secrets_backup.passphrase_hash in secrets.yaml."
    echo "Enter a passphrase (hint: recommended is the Home Pi4's login password -"
    echo "something you already know, nothing new to store). Not echoed, and the"
    echo "raw text is never written anywhere - only its SHA-256 hash is used."
    echo ""
    # Asked twice and compared. openssl used to do this itself (it prompts
    # and verifies when given no -pass); feeding it a pre-computed hash
    # bypasses that, so a typo would otherwise be silently accepted, hashed,
    # and saved - producing backups that only reveal themselves as
    # undecryptable during a real recovery, when it is far too late.
    read -r -s -p "Passphrase: " RAW_PASSPHRASE
    echo ""
    read -r -s -p "Passphrase (again): " RAW_PASSPHRASE_CONFIRM
    echo ""
    if [ "$RAW_PASSPHRASE" != "$RAW_PASSPHRASE_CONFIRM" ]; then
        echo "ERROR: the two passphrases don't match - nothing was changed." >&2
        exit 1
    fi
    if [ -z "$RAW_PASSPHRASE" ]; then
        echo "ERROR: empty passphrase refused." >&2
        exit 1
    fi
    PASSPHRASE_HASH="$(printf '%s' "$RAW_PASSPHRASE" | sha256_hex)"
    unset RAW_PASSPHRASE RAW_PASSPHRASE_CONFIRM
    echo ""
    echo "Computed hash (paste into secrets_backup.passphrase_hash in secrets.yaml"
    echo "on this machine and the Pi, so future runs don't need to re-enter it):"
    echo "  $PASSPHRASE_HASH"
    echo ""
fi

export SECRETS_BACKUP_PASSPHRASE_HASH="$PASSPHRASE_HASH"

# --- Has anything actually changed? -----------------------------------------
# Compared on the PLAINTEXT, by decrypting the existing backup - never on the
# ciphertext. openssl's -salt makes every encryption of identical input
# produce different bytes, so "did secrets.yaml.enc change in git" is always
# true and would commit+push a pointless new backup every single night.
UNCHANGED=false
if [ -f "secrets.yaml.enc" ]; then
    if secrets_openssl -d -in secrets.yaml.enc -out "$WORK_PLAIN" 2>/dev/null &&
       cmp -s "$WORK_PLAIN" "secrets.yaml"; then
        UNCHANGED=true
    fi
fi

if [ "$UNCHANGED" = true ]; then
    log "secrets.yaml is unchanged since the last backup - nothing to do."
    exit 0
fi

# --- Encrypt, then prove the result is actually recoverable ------------------
secrets_openssl -e -in secrets.yaml -out "$WORK_ENC"

# openssl exits 0 for a "successful" encryption of a truncated or empty
# input, so success alone proves nothing. Decrypting the new file back and
# comparing is what actually proves this backup can be restored - the one
# property the whole mechanism exists for.
if ! secrets_openssl -d -in "$WORK_ENC" -out "$WORK_PLAIN" 2>/dev/null ||
   ! cmp -s "$WORK_PLAIN" "secrets.yaml"; then
    echo "ERROR: the new backup did not decrypt back to secrets.yaml - refusing to" >&2
    echo "       replace the existing secrets.yaml.enc. Nothing was changed." >&2
    exit 1
fi

cp "$WORK_ENC" secrets.yaml.enc
chmod 644 secrets.yaml.enc
log "Wrote secrets.yaml.enc (verified it decrypts back to secrets.yaml)."

# --- Commit and push (unattended path only) ---------------------------------
if [ "$QUIET" != true ]; then
    echo "Now commit it:"
    echo "  git add secrets.yaml.enc && git commit -m 'Update encrypted secrets backup' && git push"
    exit 0
fi

# Scoped to this one path: a bare `git commit` commits the whole staged
# index, so anything another process happened to leave staged (this repo
# auto-commits on file edits) would be swept into a commit labelled as a
# secrets backup and pushed unreviewed.
git commit -q -m "Update encrypted secrets backup ($(date +%Y-%m-%d))" -- secrets.yaml.enc

if git push -q 2>/dev/null; then
    echo "secrets.yaml.enc changed - committed and pushed."
    exit 0
fi

# A push can legitimately fail if something else pushed first. Rebase onto
# the remote once and retry; only then treat it as a real failure. The local
# commit is already safe either way - this only affects the offsite copy.
echo "WARNING: push rejected, retrying after rebase..." >&2
if git pull --rebase -q && git push -q; then
    echo "secrets.yaml.enc changed - committed and pushed (after rebase)."
    exit 0
fi

echo "ERROR: secrets backup committed locally but could NOT be pushed." >&2
echo "       The offsite copy is now behind - resolve by hand on this machine." >&2
exit 1
