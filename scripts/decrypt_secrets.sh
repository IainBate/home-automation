#!/bin/bash
# decrypt_secrets.sh - Restore secrets.yaml from its encrypted git backup.
#
# Use this on a fresh install (setup_pi.sh calls it automatically when
# secrets.yaml is missing but secrets.yaml.enc was pulled from git), or on
# any machine that's lost its plaintext secrets.yaml. Needs the same
# passphrase used to create the backup - see encrypt_secrets.sh.
#
# Run from the repo root: bash scripts/decrypt_secrets.sh

set -e

cd "$(dirname "$0")/.."

if [ ! -f "secrets.yaml.enc" ]; then
    echo "ERROR: secrets.yaml.enc not found in $(pwd)"
    exit 1
fi

if [ -f "secrets.yaml" ]; then
    read -p "secrets.yaml already exists - overwrite it? [y/N] " confirm
    if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
        echo "Aborted. Nothing was changed."
        exit 1
    fi
fi

TMP_OUT="secrets.yaml.tmp"
trap 'rm -f "$TMP_OUT"' EXIT

openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -d \
    -in secrets.yaml.enc -out "$TMP_OUT"

mv "$TMP_OUT" secrets.yaml
chmod 600 secrets.yaml
trap - EXIT

echo "Restored secrets.yaml"
