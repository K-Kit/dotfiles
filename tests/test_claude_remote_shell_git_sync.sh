#!/usr/bin/env bash
# shellcheck shell=bash
#
# Drives the real git_push / git_pull from custom_bins/claude-remote-shell by
# sourcing it as a library (CLAUDE_REMOTE_SHELL_LIB=1) and putting a fake `ssh`
# on PATH that runs the command locally. No copy of the plumbing lives here, so
# the test cannot silently drift from the implementation.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/custom_bins/claude-remote-shell"

pass=0 fail=0
ok() {
    printf '  ✅ %s\n' "$1"
    pass=$((pass + 1))
}
no() {
    printf '  ❌ %s\n' "$1"
    fail=$((fail + 1))
}
check() { if [[ "$2" == "$3" ]]; then ok "$1"; else no "$1 (got '$2', want '$3')"; fi; }

W=$(mktemp -d "${TMPDIR:-/tmp}/crs-git-test.XXXXXX")
trap 'rm -rf "$W"' EXIT

# A stand-in for ssh: drop the -o options and the host, run the rest locally.
mkdir -p "$W/bin"
cat >"$W/bin/ssh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -o) shift 2 ;;
        -O) shift 2 ;;
        *) args+=("$1"); shift ;;
    esac
done
# args[0] is the host; git's transport appends its own command after it.
exec bash -c "${args[*]:1}"
EOF
chmod +x "$W/bin/ssh"
export PATH="$W/bin:$PATH"

LOCAL="$W/local" REMOTE="$W/remote"
export CLAUDE_REMOTE_SHELL_DIR="$W/session"
export TMPDIR="$CLAUDE_REMOTE_SHELL_DIR/tmp"
mkdir -p "$CLAUDE_REMOTE_SHELL_DIR" "$TMPDIR"

export CLAUDE_REMOTE_SYNC_MODE=git
export CLAUDE_REMOTE_HOST=testhost
export CLAUDE_REMOTE_SSH_OPTIONS="-o StrictHostKeyChecking=no"
export CLAUDE_REMOTE_GIT_ROOT="$LOCAL"
export CLAUDE_REMOTE_PATH="$REMOTE"
export CLAUDE_LOCAL_PATH="$LOCAL"
export CLAUDE_REMOTE_GIT_INDEX="$CLAUDE_REMOTE_SHELL_DIR/git-index"
export CLAUDE_REMOTE_GIT_REF="refs/claude-remote/test"
export CLAUDE_REMOTE_GIT_BACK_REF="refs/claude-remote/test-back"

git_id=(-c user.name=t -c user.email=t@invalid)

# A repo with real history, a dirty worktree, a staged change and an ignored file.
mkdir -p "$LOCAL"
git -C "$LOCAL" init -q
printf 'ignored/\n' >"$LOCAL/.gitignore"
printf 'v1\n' >"$LOCAL/tracked.txt"
printf 'gone\n' >"$LOCAL/deleteme.txt"
mkdir -p "$LOCAL/ignored"
printf 'secret\n' >"$LOCAL/ignored/.env"
git -C "$LOCAL" add -A
git -C "$LOCAL" "${git_id[@]}" commit -qm base
printf 'v2-uncommitted\n' >"$LOCAL/tracked.txt"
printf 'new\n' >"$LOCAL/untracked.txt"
git -C "$LOCAL" add "$LOCAL/deleteme.txt"

head_before=$(git -C "$LOCAL" rev-parse HEAD)
index_before=$(git -C "$LOCAL" write-tree)
branches_before=$(git -C "$LOCAL" for-each-ref --format='%(refname)' refs/heads/)

mkdir -p "$REMOTE"
git -C "$REMOTE" init -q

# shellcheck source=/dev/null
CLAUDE_REMOTE_SHELL_LIB=1 source "$SCRIPT"

echo "sync_before (git_push):"
sync_before
check "uncommitted local edit reaches the remote" "$(cat "$REMOTE/tracked.txt")" "v2-uncommitted"
if [[ -f "$REMOTE/untracked.txt" ]]; then ok "untracked file reaches the remote"; else no "untracked file missing"; fi
if [[ -e "$REMOTE/ignored/.env" ]]; then no "gitignored file leaked to the remote"; else ok "gitignored file stays local"; fi

echo "remote does work:"
printf 'built\n' >"$REMOTE/artifact.bin"
printf 'v3-from-remote\n' >"$REMOTE/tracked.txt"
rm "$REMOTE/deleteme.txt"

echo "sync_after (git_pull):"
sync_after
check "remote edit comes back" "$(cat "$LOCAL/tracked.txt")" "v3-from-remote"
if [[ -f "$LOCAL/artifact.bin" ]]; then ok "remote-created file comes back"; else no "remote-created file missing"; fi
if [[ -e "$LOCAL/deleteme.txt" ]]; then no "remote deletion did not propagate"; else ok "remote deletion propagates"; fi
check "local gitignored file untouched" "$(cat "$LOCAL/ignored/.env")" "secret"

echo "the user's repo is untouched:"
check "HEAD unchanged" "$(git -C "$LOCAL" rev-parse HEAD)" "$head_before"
check "real index unchanged" "$(git -C "$LOCAL" write-tree)" "$index_before"
check "no branches created" "$(git -C "$LOCAL" for-each-ref --format='%(refname)' refs/heads/)" "$branches_before"

echo "second round trip:"
first=$(cat "$CLAUDE_REMOTE_SHELL_DIR/last-push")
printf 'v4\n' >"$LOCAL/tracked.txt"
sync_before
check "second push lands" "$(cat "$REMOTE/tracked.txt")" "v4"
if [[ -f "$REMOTE/artifact.bin" ]]; then ok "reset --hard keeps the remote build artifact"; else no "remote artifact destroyed"; fi
check "sync commits chain" "$(git -C "$LOCAL" rev-parse "$(cat "$CLAUDE_REMOTE_SHELL_DIR/last-push")^")" "$first"

echo "no-change round trip:"
before_tree=$(git -C "$LOCAL" write-tree)
sync_after
check "identical trees leave the local worktree alone" "$(git -C "$LOCAL" write-tree)" "$before_tree"

echo
printf '%s passed, %s failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
