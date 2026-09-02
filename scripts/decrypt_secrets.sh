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
# you. See encrypt_secrets.sh for the encrypt side.
#
# Run from the repo root: bash scripts/decrypt_secrets.sh

set -e

cd "$(dirname "$0")/.."

if [ ! -f "secrets.yaml.enc" ]; then
    echo "ERROR: secrets.yaml.enc not found in $(pwd)" >&2
    exit 1
fi

if [ -f "secrets.yaml" ]; then
    read -p "secrets.yaml already exists - overwrite it? [y/N] " confirm
    if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
        echo "Aborted. Nothing was changed."
        exit 1
    fi
fi

sha256_hex() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum | cut -d' ' -f1
    else
        shasum -a 256 | cut -d' ' -f1
    fi
}

echo "Passphrase hint: this is UOY26M288's login password (unless you set a"
echo "different secrets_backup.passphrase_hash when it was encrypted)."
read -r -s -p "Passphrase: " RAW_PASSPHRASE
echo ""
PASSPHRASE_HASH="$(printf '%s' "$RAW_PASSPHRASE" | sha256_hex)"
unset RAW_PASSPHRASE

TMP_OUT="secrets.yaml.tmp"
trap 'rm -f "$TMP_OUT"' EXIT

export SECRETS_BACKUP_PASSPHRASE_HASH="$PASSPHRASE_HASH"
openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -d \
    -in secrets.yaml.enc -out "$TMP_OUT" -pass env:SECRETS_BACKUP_PASSPHRASE_HASH
unset SECRETS_BACKUP_PASSPHRASE_HASH

mv "$TMP_OUT" secrets.yaml
chmod 600 secrets.yaml
trap - EXIT

echo "Restored secrets.yaml"
