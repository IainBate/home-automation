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
# openssl passphrase. See decrypt_secrets.sh for the recovery side (same
# hint is printed there).
#
# Passphrase source, checked in this order:
#   1. secrets_backup.passphrase_hash in secrets.yaml, if set - a
#      pre-computed SHA-256 hash (this run prints one for you to save there).
#      Lets this run unattended, e.g. a daily cron job on the Home Pi4.
#   2. Otherwise, prompts interactively and hashes what you type.
#
# --quiet: for cron. Requires secrets_backup.passphrase_hash (fails rather
# than hanging on a prompt), and on success commits + pushes secrets.yaml.enc
# automatically, but only if it actually changed - an unattended daily run
# on an unchanged file is a silent no-op.
#
# Cron job setup on the Pi (needs push access on whatever SSH key `git`
# there already uses for the clone/pull in setup_pi.sh):
#   0 3 * * * cd /home/pi/home_automation && bash scripts/encrypt_secrets.sh --quiet

set -e

cd "$(dirname "$0")/.."

QUIET=false
[ "${1:-}" = "--quiet" ] && QUIET=true

if [ ! -f "secrets.yaml" ]; then
    echo "ERROR: secrets.yaml not found in $(pwd)" >&2
    exit 1
fi

sha256_hex() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum | cut -d' ' -f1
    else
        shasum -a 256 | cut -d' ' -f1
    fi
}

PYTHON="python3"
[ -x "venv/bin/python3" ] && PYTHON="venv/bin/python3"

PASSPHRASE_HASH="$("$PYTHON" -c "
import yaml
with open('secrets.yaml') as f:
    data = yaml.safe_load(f) or {}
print((data.get('secrets_backup') or {}).get('passphrase_hash') or '', end='')
" 2>/dev/null)" || PASSPHRASE_HASH=""

if [ -z "$PASSPHRASE_HASH" ]; then
    if [ "$QUIET" = true ]; then
        echo "ERROR: --quiet requires secrets_backup.passphrase_hash to be set in secrets.yaml" >&2
        exit 1
    fi
    echo "No secrets_backup.passphrase_hash in secrets.yaml."
    echo "Enter a passphrase (hint: recommended is UOY26M288's login password -"
    echo "something you already know, nothing new to store). Not echoed, and the"
    echo "raw text is never written anywhere - only its SHA-256 hash is used."
    echo ""
    read -r -s -p "Passphrase: " RAW_PASSPHRASE
    echo ""
    PASSPHRASE_HASH="$(printf '%s' "$RAW_PASSPHRASE" | sha256_hex)"
    unset RAW_PASSPHRASE
    echo ""
    echo "Computed hash (paste into secrets_backup.passphrase_hash in secrets.yaml"
    echo "on this machine and the Pi, so future runs don't need to re-enter it):"
    echo "  $PASSPHRASE_HASH"
    echo ""
fi

TMP_OUT="secrets.yaml.enc.tmp"
trap 'rm -f "$TMP_OUT"' EXIT

export SECRETS_BACKUP_PASSPHRASE_HASH="$PASSPHRASE_HASH"
openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
    -in secrets.yaml -out "$TMP_OUT" -pass env:SECRETS_BACKUP_PASSPHRASE_HASH
unset SECRETS_BACKUP_PASSPHRASE_HASH

mv "$TMP_OUT" secrets.yaml.enc
trap - EXIT

if [ "$QUIET" = true ]; then
    if git status --porcelain -- secrets.yaml.enc | grep -q .; then
        git add secrets.yaml.enc
        git commit -q -m "Update encrypted secrets backup ($(date +%Y-%m-%d))"
        git push -q
        echo "secrets.yaml.enc changed - committed and pushed."
    fi
else
    echo "Wrote secrets.yaml.enc. Now commit it:"
    echo "  git add secrets.yaml.enc && git commit -m 'Update encrypted secrets backup' && git push"
fi
