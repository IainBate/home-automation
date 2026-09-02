#!/bin/bash
# encrypt_secrets.sh - Encrypt secrets.yaml into secrets.yaml.enc for backup in git.
#
# secrets.yaml is gitignored (real credentials, never committed in plaintext),
# so if both this Mac and the Home Pi4 were lost, a fresh clone would have no
# way to recover it. secrets.yaml.enc is a deliberate exception: it IS meant
# to be committed, and is only readable with a passphrase you choose here
# (recommended: the Home Pi4's login password, since anyone who could brute
# -force this file offline could also just walk up to the Pi).
#
# Run this after every edit to secrets.yaml, then commit the result:
#   bash scripts/encrypt_secrets.sh
#   git add secrets.yaml.enc && git commit -m "Update encrypted secrets backup"
#
# To restore secrets.yaml from this backup (e.g. on a fresh install), see
# decrypt_secrets.sh.

set -e

cd "$(dirname "$0")/.."

if [ ! -f "secrets.yaml" ]; then
    echo "ERROR: secrets.yaml not found in $(pwd)"
    exit 1
fi

echo "Encrypting secrets.yaml -> secrets.yaml.enc"
echo "Enter a passphrase you'll remember and can reproduce later (e.g. the"
echo "Home Pi4's login password). It is NOT stored anywhere - if you lose"
echo "it, this backup is unrecoverable."
echo ""

TMP_OUT="secrets.yaml.enc.tmp"
trap 'rm -f "$TMP_OUT"' EXIT

openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
    -in secrets.yaml -out "$TMP_OUT"

mv "$TMP_OUT" secrets.yaml.enc
trap - EXIT

echo ""
echo "Wrote secrets.yaml.enc. Now commit it:"
echo "  git add secrets.yaml.enc && git commit -m 'Update encrypted secrets backup'"
