#!/usr/bin/env bash
# Flag committed claude-tools binaries that are older than the Rust source.
#
# Why this exists: custom_bins/claude-tools-<platform> binaries are committed,
# and the statusline runs the binary, not the source. An agent (or a human)
# editing tools/claude-tools/src/ can only rebuild the platform it is sitting
# on — nobody on Linux can cross-compile darwin-arm64 — so the usual failure is
# silent: the Mac keeps rendering the old statusline and nothing errors.
# Comments in the source cannot catch that; a date comparison can.
#
# Advisory by default (always exits 0) so it is safe to call from a hook.
# --strict exits 1 when something is stale, for use in tests or CI.
#
# Usage: check-claude-tools-fresh.sh [--strict] [--quiet]

set -euo pipefail

STRICT=false
QUIET=false
for arg in "$@"; do
    case "$arg" in
        --strict) STRICT=true ;;
        --quiet)  QUIET=true ;;
        -h|--help)
            sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "unknown option: $arg (try --help)" >&2
            exit 2
            ;;
    esac
done

REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || {
    $QUIET || echo "not in a git repository" >&2
    exit 0
}

SRC_DIR="$REPO_ROOT/tools/claude-tools"
[[ -d "$SRC_DIR" ]] || exit 0  # not the dotfiles repo

# Last commit that touched the crate (source or manifest).
src_commit_time=$(git -C "$REPO_ROOT" log -1 --format=%ct -- \
    tools/claude-tools/src tools/claude-tools/Cargo.toml 2>/dev/null || echo 0)
[[ -n "$src_commit_time" ]] || src_commit_time=0

stale=()
while IFS= read -r binary; do
    [[ -n "$binary" ]] || continue
    bin_commit_time=$(git -C "$REPO_ROOT" log -1 --format=%ct -- "$binary" 2>/dev/null || echo 0)
    [[ -n "$bin_commit_time" ]] || bin_commit_time=0
    if (( src_commit_time > bin_commit_time )); then
        stale+=("$(basename "$binary")")
    fi
done < <(git -C "$REPO_ROOT" ls-files 'custom_bins/claude-tools-*')

# Uncommitted source edits are a softer signal: nothing is stale *yet*, but
# every binary will be the moment the edit is committed.
dirty_src=$(git -C "$REPO_ROOT" status --porcelain -- tools/claude-tools/src 2>/dev/null)

if [[ ${#stale[@]} -eq 0 && -z "$dirty_src" ]]; then
    $QUIET || echo "claude-tools binaries are up to date with the source"
    exit 0
fi

if [[ -n "$dirty_src" ]]; then
    echo "⚠ tools/claude-tools/src has uncommitted changes — every committed binary will be stale once you commit."
fi

if [[ ${#stale[@]} -gt 0 ]]; then
    echo "⚠ claude-tools binaries older than the Rust source: ${stale[*]}"
    echo "  Rebuild on each platform (they cannot be cross-compiled here):"
    echo "    cargo build --release --manifest-path tools/claude-tools/Cargo.toml"
    echo "    cp tools/claude-tools/target/release/claude-tools custom_bins/claude-tools-<platform>"
fi

$STRICT && exit 1
exit 0
