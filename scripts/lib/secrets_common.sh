# secrets_common.sh - shared helpers for encrypt_secrets.sh / decrypt_secrets.sh.
#
# Sourced, never executed. These two scripts MUST agree exactly on how a
# passphrase becomes an openssl key: openssl does not fail loudly on a wrong
# key in a way that says "wrong key" - a mismatch surfaces as a decrypt error
# during an actual disaster recovery, which is the worst possible moment to
# discover the two sides drifted apart. Keeping the shared parts here means
# there is one definition to change, not two to remember to change together.

# SHA-256 of stdin, printed as lowercase hex with no trailing filename.
# The raw passphrase is never used as the openssl key directly and is never
# written anywhere - only this hash is.
sha256_hex() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum | cut -d' ' -f1
    else
        shasum -a 256 | cut -d' ' -f1
    fi
}

# The openssl invocation, shared so the cipher/KDF/iteration count can never
# drift between encrypt and decrypt. Args: -e|-d, then -in/-out pairs.
# Reads the key from SECRETS_BACKUP_PASSPHRASE_HASH in the environment.
secrets_openssl() {
    openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt "$@" \
        -pass env:SECRETS_BACKUP_PASSPHRASE_HASH
}

# Read the pre-computed hash out of secrets.yaml's secrets_backup section,
# or print nothing if absent/unreadable. Run from the repo root.
read_configured_passphrase_hash() {
    local python_bin="python3"
    [ -x "venv/bin/python3" ] && python_bin="venv/bin/python3"
    "$python_bin" -c "
import yaml
try:
    with open('secrets.yaml') as f:
        data = yaml.safe_load(f) or {}
except Exception:
    data = {}
print((data.get('secrets_backup') or {}).get('passphrase_hash') or '', end='')
" 2>/dev/null || true
}
