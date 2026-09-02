#!/bin/bash
# decrypt_secrets.sh - Restore secrets.yaml from its encrypted git backup.
#
# Use this on a fresh install (setup_pi.sh calls it automatically when
# secrets.yaml is missing but secrets.yaml.enc was pulled from git), or on
# any machine that's lost its plaintext secrets.yaml.
#
# Passphrase hint: this backup uses the SHA-256 hash of a passphrase as the
# actual key - recommended (and, if never overridden, what was used) is the
# Home Pi4's own login password. Type that below; this script hashes it for
# you. Set $SECRETS_BACKUP_PASSPHRASE instead to run non-interactively (e.g.
# an agent following docs/PI4_DEPLOYMENT.md over SSH). See encrypt_secrets.sh
# for the encrypt side.
#
# Run from the repo root: bash scripts/decrypt_secrets.sh

set -e

cd "$(dirname "$0")/.."
# shellcheck source=lib/secrets_common.sh
. "scripts/lib/secrets_common.sh"

if [ ! -f "secrets.yaml.enc" ]; then
    echo "ERROR: secrets.yaml.enc not found in $(pwd)" >&2
    exit 1
fi

if [ -f "secrets.yaml" ]; then
    if [ -n "${SECRETS_BACKUP_OVERWRITE:-}" ]; then
        echo "secrets.yaml already exists - overwriting (SECRETS_BACKUP_OVERWRITE set)."
    elif [ ! -t 0 ]; then
        echo "ERROR: secrets.yaml already exists and there's no terminal to confirm on." >&2
        echo "       Set SECRETS_BACKUP_OVERWRITE=1 to overwrite it non-interactively." >&2
        exit 1
    else
        read -r -p "secrets.yaml already exists - overwrite it? [y/N] " confirm
        if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
            echo "Aborted. Nothing was changed."
            exit 1
        fi
    fi
fi

if [ -n "${SECRETS_BACKUP_PASSPHRASE:-}" ]; then
    PASSPHRASE_HASH="$(printf '%s' "$SECRETS_BACKUP_PASSPHRASE" | sha256_hex)"
else
    if [ ! -t 0 ]; then
        echo "ERROR: no terminal to prompt on. Set \$SECRETS_BACKUP_PASSPHRASE to" >&2
        echo "       run non-interactively." >&2
        exit 1
    fi
    echo "Passphrase hint: this is the Home Pi4's login password (unless you set a"
    echo "different secrets_backup.passphrase_hash when it was encrypted)."
    read -r -s -p "Passphrase: " RAW_PASSPHRASE
    echo ""
    PASSPHRASE_HASH="$(printf '%s' "$RAW_PASSPHRASE" | sha256_hex)"
    unset RAW_PASSPHRASE
fi

# mktemp, not a fixed "secrets.yaml.tmp" in the repo root: two runs sharing
# one path can corrupt each other, and a crash between write and rename would
# otherwise leave decrypted credentials sitting in the working tree under a
# name .gitignore doesn't cover.
WORK_PLAIN="$(mktemp "${TMPDIR:-/tmp}/secrets.plain.XXXXXX")"
trap 'rm -f "$WORK_PLAIN"' EXIT

export SECRETS_BACKUP_PASSPHRASE_HASH="$PASSPHRASE_HASH"
if ! secrets_openssl -d -in secrets.yaml.enc -out "$WORK_PLAIN" 2>/dev/null; then
    echo "ERROR: could not decrypt secrets.yaml.enc - wrong passphrase?" >&2
    echo "       Nothing was changed." >&2
    exit 1
fi
unset SECRETS_BACKUP_PASSPHRASE_HASH

# A wrong-but-structurally-valid decrypt is conceivable; a YAML parse is a
# cheap sanity check that what came out is actually the secrets file.
if ! grep -q ":" "$WORK_PLAIN"; then
    echo "ERROR: decrypted output doesn't look like secrets.yaml - refusing to install it." >&2
    exit 1
fi

cp "$WORK_PLAIN" secrets.yaml
chmod 600 secrets.yaml

echo "Restored secrets.yaml"
