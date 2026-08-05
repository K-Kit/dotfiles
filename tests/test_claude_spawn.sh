#!/usr/bin/env bash
# Tests for custom_bins/claude-spawn.
#
# All assertions run through --dry-run, so no tmux session is ever created and
# the test is safe to run anywhere. The gates are the point of this file: if a
# refusal stops firing, a spawned agent gets more capability than intended.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPAWN="$SCRIPT_DIR/../custom_bins/claude-spawn"

pass=0
fail=0

ok()   { printf '  ok   %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf '  FAIL %s\n     %s\n' "$1" "$2"; fail=$((fail + 1)); }

# assert_contains <name> <expected-substring> <actual>
assert_contains() {
  if [[ "$3" == *"$2"* ]]; then ok "$1"; else bad "$1" "expected to contain: $2"; fi
}

# assert_not_contains <name> <forbidden-substring> <actual>
assert_not_contains() {
  if [[ "$3" != *"$2"* ]]; then ok "$1"; else bad "$1" "must not contain: $2"; fi
}

# assert_exit <name> <expected-code> <actual-code>
assert_exit() {
  if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1" "expected exit $2, got $3"; fi
}

printf 'claude-spawn\n'

# --- defaults ---------------------------------------------------------------

out=$("$SPAWN" --dry-run "seed prompt" 2>&1)
assert_contains     "default: builds a claude command"  "zsh -ic 'claude " "$out"
assert_contains     "default: remote control disabled"  "remote control: disabled" "$out"
assert_not_contains "default: no --remote-control flag" "--remote-control" "$out"
assert_not_contains "default: no skip-permissions"      "--dangerously-skip-permissions" "$out"

# The prompt must reach the command UNEXPANDED — if bash or sh expands it here,
# the seed text would be subject to two more rounds of word splitting.
# shellcheck disable=SC2016  # asserting the literal, unexpanded text
assert_contains "prompt is deferred, not interpolated" '"$CLAUDE_SPAWN_PROMPT"' "$out"
assert_not_contains "prompt text absent from command" "seed prompt\"; exec" "$out"

# Interactive zsh is required or the claude() wrapper is silently missing.
assert_contains "uses an interactive shell" "zsh -ic" "$out"

# --- remote control is opt-in ----------------------------------------------

out=$("$SPAWN" --dry-run -r "x" 2>&1)
assert_contains "-r enables remote control" "--remote-control" "$out"

# `--remote-control [name]` takes an OPTIONAL argument, so a bare
# `--remote-control` followed by the prompt would consume the prompt as the
# session name and start an agent with no seed at all. The name must always be
# supplied explicitly. This assertion is the guard on that invariant.
# shellcheck disable=SC2016
assert_contains "remote control is never bare" '--remote-control "$CLAUDE_SPAWN_RC_NAME"' "$out"

out=$("$SPAWN" --dry-run -n my-rc-name "x" 2>&1)
assert_contains "-n implies remote control" "--remote-control" "$out"
assert_contains "-n sets the name"          "remote control: my-rc-name" "$out"

# --- gate: --yolo + --remote-control ----------------------------------------

out=$("$SPAWN" --dry-run -y -r "x" 2>&1); code=$?
assert_exit     "gate: yolo+rc refused"        4 "$code"
assert_contains "gate: yolo+rc explains why"   "drivable from off-machine" "$out"

out=$("$SPAWN" --dry-run -y -r --allow-remote-yolo "x" 2>&1); code=$?
assert_exit     "gate: yolo+rc overridable"    0 "$code"
assert_contains "gate: override actually yolos" "--dangerously-skip-permissions" "$out"

# Either alone is ordinary and must not be blocked.
out=$("$SPAWN" --dry-run -y "x" 2>&1); code=$?
assert_exit     "yolo alone allowed"  0 "$code"
assert_contains "yolo alone skips perms" "--dangerously-skip-permissions" "$out"

# --- gate: recursion depth --------------------------------------------------

out=$(CLAUDE_SPAWN_DEPTH=1 "$SPAWN" --dry-run "x" 2>&1); code=$?
assert_exit     "gate: nested spawn refused"    4 "$code"
assert_contains "gate: nested names the depth"  "depth=1" "$out"

out=$(CLAUDE_SPAWN_DEPTH=1 "$SPAWN" --dry-run --allow-nested "x" 2>&1); code=$?
assert_exit "gate: nested overridable" 0 "$code"

out=$(CLAUDE_SPAWN_DEPTH=0 "$SPAWN" --dry-run "x" 2>&1); code=$?
assert_exit "depth 0 is not nested" 0 "$code"

# --- argument handling ------------------------------------------------------

out=$("$SPAWN" --dry-run 2>&1); code=$?
assert_exit     "missing prompt is an error" 1 "$code"
assert_contains "missing prompt explains"    "no seed prompt" "$out"

out=$("$SPAWN" --dry-run -d /nonexistent-dir-xyz "x" 2>&1); code=$?
assert_exit     "bad --dir is an error" 1 "$code"
assert_contains "bad --dir explains"    "not a directory" "$out"

out=$("$SPAWN" --dry-run --bogus-flag "x" 2>&1); code=$?
assert_exit "unknown flag is an error" 1 "$code"

out=$(printf 'from stdin' | "$SPAWN" --dry-run - 2>&1); code=$?
assert_exit     "stdin prompt accepted" 0 "$code"
assert_contains "stdin prompt has length" "prompt bytes:   10" "$out"

# Session names must not contain tmux target separators.
out=$("$SPAWN" --dry-run -s 'has.dots:and:colons' "x" 2>&1)
assert_not_contains "session name strips dots"   "has.dots" "$out"
assert_not_contains "session name strips colons" ":and:" "$out"

out=$("$SPAWN" --dry-run -h 2>&1); code=$?
assert_exit     "--help exits 0" 0 "$code"
assert_contains "--help lists exit codes" "Exit codes:" "$out"

# --- dry-run must not have side effects -------------------------------------

log="${XDG_STATE_HOME:-$HOME/.local/state}/claude-spawn/spawn.log"
before=$( [[ -f "$log" ]] && wc -l <"$log" || echo 0 )
"$SPAWN" --dry-run "x" >/dev/null 2>&1
after=$( [[ -f "$log" ]] && wc -l <"$log" || echo 0 )
if [[ "$before" == "$after" ]]; then
  ok "dry-run writes no audit entry"
else
  bad "dry-run writes no audit entry" "audit log grew from $before to $after lines"
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
