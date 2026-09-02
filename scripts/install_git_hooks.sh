#!/bin/bash
# install_git_hooks.sh - install this repo's git hooks into .git/hooks/.
#
# Git doesn't track .git/hooks/, so the hooks themselves live in
# scripts/git-hooks/ (version-controlled) and this script points git at them.
# Safe to re-run: it replaces only hooks this repo owns, and refuses to
# clobber an unrelated existing hook without being told to.
#
#   bash scripts/install_git_hooks.sh          # install
#   FORCE=1 bash scripts/install_git_hooks.sh  # overwrite existing hooks

set -e

cd "$(dirname "$0")/.."

HOOKS_SRC="scripts/git-hooks"
HOOKS_DIR="$(git rev-parse --git-path hooks)"
mkdir -p "$HOOKS_DIR"

installed=0
for src in "$HOOKS_SRC"/*; do
    [ -f "$src" ] || continue
    name="$(basename "$src")"
    dest="$HOOKS_DIR/$name"

    if [ -e "$dest" ] && ! grep -q "scripts/install_git_hooks.sh" "$dest" 2>/dev/null; then
        if [ -z "${FORCE:-}" ]; then
            echo "SKIPPED $name - a different hook is already installed at $dest"
            echo "        (re-run with FORCE=1 to replace it)"
            continue
        fi
        cp "$dest" "$dest.replaced.$(date +%Y%m%d%H%M%S)"
        echo "Backed up the existing $name hook before replacing it."
    fi

    # Copied, not symlinked: a symlink into the working tree would silently
    # change behaviour on checkout of another branch, and git refuses to run
    # hooks it can't execute.
    {
        echo "#!/bin/bash"
        echo "# Installed by scripts/install_git_hooks.sh - edit $src, then re-run that."
        tail -n +2 "$src"
    } > "$dest"
    chmod +x "$dest"
    echo "Installed $name -> $dest"
    installed=$((installed + 1))
done

echo "$installed hook(s) installed."
