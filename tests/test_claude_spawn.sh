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

# --- the emitted argv actually works ----------------------------------------
#
# Everything above greps a printed string, which only proves the script says the
# right thing. This runs what the script emits, with a stub on PATH standing in
# for the real agent, and reads back what the agent would have received. That
# closes the gap between "I believe it emits X" and "X does the right thing".
#
# Skipped without a reachable tmux server (CI, or a sandbox that denies the
# tmux socket) — skipping is reported, never silently passed.

probe_dir=$(mktemp -d 2>/dev/null || echo "")
probe_session="claude-spawn-argvprobe-$$"

# Without this guard an unwritable TMPDIR leaves probe_dir empty, and every
# "$probe_dir/..." below becomes an absolute path at the filesystem root — the
# stub would be written to /bin/claude. Refuse rather than build on an empty
# prefix.
if [[ -z "$probe_dir" || ! -d "$probe_dir" ]]; then
  printf '  SKIP argv probe (no writable temp directory)\n'
  printf '\n%d passed, %d failed\n' "$pass" "$fail"
  [[ "$fail" -eq 0 ]]
  exit $?
fi

# A private tmux server, so the probe cannot collide with, inherit from, or
# leave anything behind in the user's real one. TMUX_TMPDIR picks the socket
# directory, and the eval'd command inherits it, so it reaches this server too.
export TMUX_TMPDIR="$probe_dir/tmux"
mkdir -p "$TMUX_TMPDIR"
chmod 700 "$TMUX_TMPDIR"

# Stub `claude`, recording what the agent would actually have received.
mkdir -p "$probe_dir/bin"
{
  printf '#!/usr/bin/env bash\n'
  printf 'printf "ARG:%%s\\n" "$@" >"%s/argv.txt"\n' "$probe_dir"
  # shellcheck disable=SC2016  # writing a script; expansion happens when it runs
  printf 'printf "DEPTH:%%s\\n" "${CLAUDE_SPAWN_DEPTH:-unset}" >>"%s/argv.txt"\n' "$probe_dir"
} >"$probe_dir/bin/claude"
chmod +x "$probe_dir/bin/claude"

# The real claude() is a zsh *function*, and a function beats a PATH entry. An
# empty ZDOTDIR means `zsh -ic` defines no such function, so the stub wins and
# the probe never launches a real agent.
mkdir -p "$probe_dir/zdot"
: >"$probe_dir/zdot/.zshrc"
: >"$probe_dir/zdot/.zshenv"

# The server inherits this environment at start, and passes it to every pane —
# PATH and ZDOTDIR are not in tmux's update-environment, so seeding them here is
# the way they reach the agent.
if ! PATH="$probe_dir/bin:$PATH" ZDOTDIR="$probe_dir/zdot" tmux start-server 2>/dev/null; then
  printf '  SKIP argv probe (cannot start a tmux server here)\n'
else
  # Deliberately hostile: spaces, double quotes, and a `$var` that must survive
  # every shell layer unexpanded.
  # shellcheck disable=SC2016
  probe_prompt='multi word "quoted" $notexpanded'

  # Ask the script for its own tmux invocation, then run exactly that. This is
  # the whole point: no hand-transcribed copy sits between test and artifact.
  argv=$("$SPAWN" --print-tmux-command -s "$probe_session" -d "$probe_dir" "$probe_prompt" 2>&1)

  if [[ "$argv" != tmux\ * ]]; then
    bad "argv probe: script emits a tmux command" "got: $argv"
  else
    eval "$argv" 2>/dev/null

    for _ in 1 2 3 4 5 6 7 8 9 10; do
      [[ -s "$probe_dir/argv.txt" ]] && break
      sleep 0.5
    done

    got=$(cat "$probe_dir/argv.txt" 2>/dev/null || echo "")

    if [[ -z "$got" ]]; then
      bad "argv probe: the emitted command reaches the agent" "stub was never invoked"
    else
      assert_contains     "argv probe: prompt arrives verbatim" "ARG:$probe_prompt" "$got"
      assert_contains     "argv probe: depth propagates"        "DEPTH:1" "$got"
      assert_not_contains "argv probe: no stray flags"          "ARG:--" "$got"
    fi
  fi
  tmux kill-server 2>/dev/null || true
fi

rm -rf "$probe_dir"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
