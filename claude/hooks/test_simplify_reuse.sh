#!/usr/bin/env bash
# Tests for the scratch-script reuse nudge: simplify_track_reuse.py (counts
# repeat runs) and simplify_nudge.sh (turns the tally into a promotion nudge).
#
# Same contract as test_convention_nudges.sh: each hook must (a) fire a
# systemMessage on a positive case, (b) stay silent on negatives, and
# (c) NEVER exit non-zero.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PASS=0
FAIL=0

TMP=""
for cand in "${TMPDIR:-}" /tmp/claude /tmp .; do
    [ -n "$cand" ] || continue
    if mkdir -p "$cand/simplify-reuse-tests.$$" 2>/dev/null; then
        TMP="$cand/simplify-reuse-tests.$$"
        break
    fi
done
[ -n "$TMP" ] || { echo "no writable temp dir found" >&2; exit 1; }
trap 'rm -rf "$TMP"' EXIT

# Hooks keep their state under TMPDIR — point them at the sandbox.
export TMPDIR="$TMP"

ok()   { PASS=$((PASS + 1)); }
bad()  { FAIL=$((FAIL + 1)); printf 'FAIL: %s\n' "$1"; }

# bash_event <session> <cwd> <command>
bash_event() {
    python3 -c "
import json, sys
session, cwd, command = sys.argv[1:4]
print(json.dumps({'session_id': session, 'cwd': cwd, 'tool_name': 'Bash',
                  'tool_input': {'command': command}}))
" "$1" "$2" "$3"
}

# track <session> <cwd> <command> — one run through the PostToolUse tracker.
track() {
    local rc=0
    bash_event "$1" "$2" "$3" | python3 "$DIR/simplify_track_reuse.py" >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 0 ] || bad "tracker exited $rc on: $3"
}

# nudge <session> — run the Stop hook, echo its stdout.
nudge() {
    local rc=0 out
    out=$(printf '{"session_id":"%s"}' "$1" | bash "$DIR/simplify_nudge.sh" 2>/dev/null) || rc=$?
    [ "$rc" -eq 0 ] || bad "nudge hook exited $rc"
    printf '%s' "$out"
}

# expect <desc> <output> <fire|silent> [needle]
expect() {
    local desc="$1" out="$2" want="$3" needle="${4:-}"
    local got=silent
    case "$out" in *systemMessage*) got=fire ;; esac
    if [ "$got" != "$want" ]; then
        bad "$desc (expected $want, got $got)"
        return
    fi
    if [ -n "$needle" ] && [[ "$out" != *"$needle"* ]]; then
        bad "$desc (missing '$needle' in message)"
        return
    fi
    ok
}

runs() { local n=$1; shift; while [ "$n" -gt 0 ]; do track "$@"; n=$((n - 1)); done; }

# --- tracker: what counts as a run of a scratch script ----------------------
echo "=== reuse tracking ==="

mkdir -p "$TMP/scratch" "$TMP/bin" "$TMP/node_modules/pkg/tmp"
printf 'print("hi")\n' > "$TMP/scratch/probe.py"
printf 'echo hi\n'     > "$TMP/scratch/probe.sh"
printf 'echo hi\n'     > "$TMP/bin/deploy.sh"
printf 'x = 1\n'       > "$TMP/tmp_backfill.py"
printf 'x = 1\n'       > "$TMP/node_modules/pkg/tmp/setup.py"

S=count-py
runs 3 "$S" "$TMP" "python3 $TMP/scratch/probe.py --limit 3"
expect "3 runs of a scratch script fires" "$(nudge "$S")" fire "probe.py"

S=count-uv
runs 3 "$S" "$TMP" "uv run $TMP/scratch/probe.py"
expect "uv run counts as execution" "$(nudge "$S")" fire "probe.py"

S=count-direct
runs 3 "$S" "$TMP" "$TMP/scratch/probe.sh"
expect "direct invocation counts" "$(nudge "$S")" fire "probe.sh"

S=count-relative
runs 3 "$S" "$TMP" "bash scratch/probe.sh"
expect "relative path resolves against cwd" "$(nudge "$S")" fire "$TMP/scratch/probe.sh"

S=count-name
runs 3 "$S" "$TMP" "python3 $TMP/tmp_backfill.py"
expect "tmp_-prefixed name counts as scratch" "$(nudge "$S")" fire "tmp_backfill.py"

S=below
runs 2 "$S" "$TMP" "python3 $TMP/scratch/probe.py"
expect "2 runs stays below threshold" "$(nudge "$S")" silent

S=permanent
runs 5 "$S" "$TMP" "bash $TMP/bin/deploy.sh"
expect "script in a permanent home ignored" "$(nudge "$S")" silent

S=vendored
runs 5 "$S" "$TMP" "python3 $TMP/node_modules/pkg/tmp/setup.py"
expect "vendored tmp/ path ignored" "$(nudge "$S")" silent

S=notrun
runs 5 "$S" "$TMP" "cat $TMP/scratch/probe.py"
expect "reading a script is not running it" "$(nudge "$S")" silent

S=redirect
runs 5 "$S" "$TMP" "echo 'print(1)' > $TMP/scratch/probe.py"
expect "writing a script is not running it" "$(nudge "$S")" silent

# Edit-run-edit-run debugging: every run sees a different mtime, so no run is
# "stable" and the script never reaches promotion.
S=churn
for i in 1 2 3 4; do
    printf 'print(%d)\n' "$i" > "$TMP/scratch/churn.py"
    touch -t "0101010${i}00" "$TMP/scratch/churn.py"
    track "$S" "$TMP" "python3 $TMP/scratch/churn.py"
done
expect "edited between every run stays silent" "$(nudge "$S")" silent

# --- nudge: message composition and repeat suppression ----------------------
echo "=== nudge behaviour ==="

S=once
runs 3 "$S" "$TMP" "python3 $TMP/scratch/probe.py"
expect "first stop nudges" "$(nudge "$S")" fire "probe.py"
expect "second stop stays quiet" "$(nudge "$S")" silent
track "$S" "$TMP" "python3 $TMP/scratch/probe.py"
expect "already-promoted script stays quiet" "$(nudge "$S")" silent

S=dirty
touch "$TMP/claude-simplify-dirty-${S}"
expect "dirty marker alone still nudges" "$(nudge "$S")" fire "quality pass"

S=both
touch "$TMP/claude-simplify-dirty-${S}"
runs 3 "$S" "$TMP" "python3 $TMP/scratch/probe.py"
OUT=$(nudge "$S")
expect "both signals: quality pass" "$OUT" fire "quality pass"
expect "both signals: promotion"    "$OUT" fire "probe.py"

expect "clean session silent" "$(nudge no-such-session)" silent

# --- robustness: hooks must never fail the tool call ------------------------
echo "=== robustness ==="

for payload in '' 'not json' '[]' '{}' '{"session_id":"x"}' \
               '{"session_id":"x","tool_input":{"command":"python3 \"unbalanced}'; do
    rc=0
    printf '%s' "$payload" | python3 "$DIR/simplify_track_reuse.py" >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 0 ] && ok || bad "tracker exited $rc on payload: $payload"
    rc=0
    printf '%s' "$payload" | bash "$DIR/simplify_nudge.sh" >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 0 ] && ok || bad "nudge exited $rc on payload: $payload"
done

# A corrupt state file must degrade to "no candidates", not to an error.
S=corrupt
printf 'not json at all' > "$TMP/claude-simplify-reuse-${S}.json"
expect "corrupt state file silent" "$(nudge "$S")" silent
track "$S" "$TMP" "python3 $TMP/scratch/probe.py"
ok  # tracker survived a corrupt state file (bad() already fired if it didn't)

echo
TOTAL=$((PASS + FAIL))
echo "Results: $PASS passed, $FAIL failed (total $TOTAL)"
[ "$FAIL" -eq 0 ] && echo "All tests passed!"
[ "$FAIL" -eq 0 ]
