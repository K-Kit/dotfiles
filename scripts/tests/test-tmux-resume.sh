#!/usr/bin/env bash
# Functional test for tmux-resume's detect-only cooldown state machine, its
# opt-in send gate, and the send-sequence parser. Self-contained: builds its own
# fixtures under a temp dir.
#
#   bash scripts/tests/test-tmux-resume.sh
#
# Exits non-zero on the first failed assertion.
#
# WHY THIS EXISTS: config/tmux-resume-patterns.conf says the ACTION half of each
# row is fragile and must be re-verified after CLI upgrades. That check is only
# cheap if the harness is checked in, so it is.
#
# WHAT IT CANNOT COVER: whether the keystrokes actually drive a live rate-limit
# prompt. That needs `tmux-resume --dry-run` against a real rate-limited pane.
# This tests the machinery around the keystrokes, not the keystrokes' effect.
#
# TWO ACCOMMODATIONS, both harness-side, neither touching the logic under test:
#
# 1. A sandboxed session cannot socket(AF_UNIX), so no real tmux server socket
#    can exist and discover_sockets() would find nothing. We build a copy of
#    tmux-resume with ONE added line: a second discover_sockets() definition
#    (later definition wins in bash) echoing a fixed fake path.
# 2. tmux-resume deliberately re-exports PATH with system dirs FIRST (cron gives
#    a minimal PATH), so a stub earlier in the caller's PATH loses to
#    /usr/bin/tmux. The first entry it prepends is "$HOME/.local/bin", so we
#    point HOME at a test dir and install the stub there — the stub wins by the
#    script's own ordering rather than by editing the script.
#
# Everything under test — pattern parsing, first-match ordering, the cooldown
# state machine, dry-run guarding, the send loop — runs unmodified.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# $TMPDIR is not reliably writable — under Claude Code's sandbox it points at
# /run/user/$UID, which is read-only. Take the first base that actually works.
T=""
for base in "${TMPDIR:-}" /tmp/claude /tmp; do
  [[ -n "$base" && -d "$base" && -w "$base" ]] || continue
  T="$(mktemp -d "$base/tmux-resume-test.XXXXXX" 2>/dev/null)" && break
done
[[ -n "$T" ]] || { echo "no writable temp dir (tried \$TMPDIR, /tmp/claude, /tmp)"; exit 1; }
trap 'rm -rf "$T"' EXIT

FAILED=0
pass() { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n' "$1"; FAILED=1; }
# assert_eq <label> <expected> <actual>
assert_eq() {
  if [[ "$2" == "$3" ]]; then pass "$1"
  else fail "$1"; printf '       expected: %q\n       actual:   %q\n' "$2" "$3"; fi
}

mkdir -p "$T/state" "$T/home/.local/bin"

cat > "$T/home/.local/bin/tmux" <<'STUB'
#!/usr/bin/env bash
# Stub tmux. Drops the leading "-S <socket>" that tmux-resume always passes.
shift 2
case "$1" in
  # tmux-resume asks for "target<TAB>window_name". Emit both, tab-separated —
  # a single field would leave the window name empty, which the opt-in gate
  # reads as "not opted in" and every send assertion below would fail.
  list-panes)   printf '%s\t%s\n' "${FAKE_TARGET:-fake:0.0}" "${FAKE_WINDOW:-auto-test}" ;;
  capture-pane) cat "$FAKE_CAPTURE" ;;
  send-keys)    shift; echo "SEND: $*" >> "$FAKE_SENDLOG" ;;
esac
STUB
chmod +x "$T/home/.local/bin/tmux"

python3 - "$REPO/custom_bins/tmux-resume" "$T/tmux-resume-test" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()
anchor = "resume_pane() {"
assert text.count(anchor) == 1, "anchor 'resume_pane() {' not found exactly once"
open(dst, "w").write(text.replace(anchor, 'discover_sockets() { echo "/fake/tmux-socket"; }\n\n' + anchor))
PY
chmod +x "$T/tmux-resume-test"

# Real captured wording (2026-07-27), the case the rows are tuned for.
printf '%s\n' \
  'Usage limit reached' \
  "You've hit your weekly limit · resets Jul 29, 3pm (UTC)" \
  > "$T/capture.txt"
# A pane that must NOT match: both strings are near-misses the guards exclude.
printf '%s\n' \
  'git reset HEAD~1' \
  'rate limiter configured with limit=500' \
  > "$T/benign.txt"

export HOME="$T/home"
export TMUX_RESUME_PATTERNS="$REPO/config/tmux-resume-patterns.conf"
export TMUX_RESUME_STATE_DIR="$T/state"
export FAKE_CAPTURE="$T/capture.txt"
export FAKE_SENDLOG="$T/sends.log"
# Opted in by default so the cooldown/parser cases below exercise the send path.
# The gate itself is tested explicitly further down, both ways.
export FAKE_WINDOW="auto-test"
: > "$T/sends.log"

run() { "$T/tmux-resume-test" "$@" 2>&1 | sed 's/^\[[^]]*\] //'; }
sends() { cat "$T/sends.log"; }
reset_state() { rm -f "$T"/state/* 2>/dev/null; : > "$T/sends.log"; }

# The exact keystroke decomposition the config's action row must produce.
# `-l` on the middle line is load-bearing: it disables tmux key-name lookup, so
# the `-` in `wrap-up` cannot be read as a modifier separator.
EXPECTED_SEND='SEND: -t fake:0.0 1 Enter
SEND: -t fake:0.0 -l /wrap-up
SEND: -t fake:0.0 Enter Enter'

echo "== detect-only cooldown =="
out="$(run)"
assert_eq "first sighting holds, does not send" "yes" \
  "$([[ "$out" == *"first sighting, holding"* && ! -s "$T/sends.log" ]] && echo yes || echo no)"

out="$(run)"
assert_eq "second sighting still holds" "yes" \
  "$([[ "$out" == *"holding ("* && ! -s "$T/sends.log" ]] && echo yes || echo no)"

assert_eq "exactly one marker file" "1" "$(find "$T/state" -type f | wc -l | tr -d ' ')"

echo "== fall-through after cooldown =="
# Backdate the marker's mtime, which is the cooldown clock. NOT `touch -d '7 hours
# ago'` — relative -d is GNU-only and this file is meant to be re-run on macOS too,
# where BSD touch takes -t [[CC]YY]MMDDhhmm instead. python3 is already a
# dependency of this harness, so use it and stay portable.
python3 -c 'import os,sys,time; t=time.time()-7*3600; [os.utime(p,(t,t)) for p in sys.argv[1:]]' "$T"/state/*
out="$(run)"
assert_eq "cooldown elapsed -> falls through to action row" "yes" \
  "$([[ "$out" == *"cooldown elapsed"* && "$out" == *"sending:"* ]] && echo yes || echo no)"
assert_eq "keystrokes sent verbatim" "$EXPECTED_SEND" "$(sends)"

out="$(run)"
assert_eq "clock restarts after fall-through" "yes" \
  "$([[ "$out" == *"holding ("* ]] && echo yes || echo no)"
assert_eq "no second burst" "$EXPECTED_SEND" "$(sends)"

echo "== dry-run is inert =="
reset_state
run --dry-run > /dev/null
assert_eq "dry-run writes no marker" "0" "$(find "$T/state" -type f | wc -l | tr -d ' ')"
assert_eq "dry-run sends nothing" "" "$(sends)"

echo "== cooldown escape hatch =="
reset_state
out="$(TMUX_RESUME_DETECT_COOLDOWN_HOURS=0 "$T/tmux-resume-test" 2>&1)"
assert_eq "cooldown 0 -> immediate fall-through" "yes" \
  "$([[ "$out" == *"cooldown 0"* ]] && echo yes || echo no)"
assert_eq "cooldown 0 sends keystrokes" "$EXPECTED_SEND" "$(sends)"

echo "== opt-in gate =="
# All of these use cooldown 0 so the detect-only row falls straight through and
# the action row — the only row that can send — is the one under test.
gated() { reset_state; TMUX_RESUME_DETECT_COOLDOWN_HOURS=0 "$T/tmux-resume-test" 2>&1; }

out="$(FAKE_WINDOW="dotfiles" gated)"
assert_eq "un-opted window still detected" "yes" \
  "$([[ "$out" == *MATCH* ]] && echo yes || echo no)"
assert_eq "un-opted window says why it sent nothing" "yes" \
  "$([[ "$out" == *"not opted in"* ]] && echo yes || echo no)"
assert_eq "un-opted window receives nothing" "" "$(sends)"

out="$(FAKE_WINDOW="auto-overnight" gated)"
assert_eq "opted-in window receives keystrokes" "$EXPECTED_SEND" "$(sends)"

# Window names can contain spaces; the pane list is tab-delimited so they survive.
out="$(FAKE_WINDOW="auto-two words" gated)"
assert_eq "opted-in name with a space still opts in" "$EXPECTED_SEND" "$(sends)"

# Env overrides the file directive, so a different prefix can opt a window in.
out="$(FAKE_WINDOW="ci-run" TMUX_RESUME_OPTIN_PREFIX="ci-" gated)"
assert_eq "env prefix overrides the file directive" "$EXPECTED_SEND" "$(sends)"

# Set-but-empty must FAIL CLOSED. `${VAR:-default}` would treat it as unset and
# silently restore `auto-`, i.e. keep sending after being told to stop.
out="$(FAKE_WINDOW="auto-test" TMUX_RESUME_OPTIN_PREFIX="" gated)"
assert_eq "empty prefix disables sending, does not match everything" "" "$(sends)"
assert_eq "empty prefix reports no prefix" "yes" \
  "$([[ "$out" == *"<none>"* ]] && echo yes || echo no)"

echo "== directive lines are not pattern rows =="
# A directive has no " | ", so the row parser would read the whole line as both
# name and regex — turning any pane that displays the directive text into a
# match. It is skipped instead; this pane must stay untouched.
reset_state
printf '%s\n' 'TMUX_RESUME_OPTIN_PREFIX=auto-' > "$T/directive.txt"
out="$(FAKE_CAPTURE="$T/directive.txt" TMUX_RESUME_DETECT_COOLDOWN_HOURS=0 "$T/tmux-resume-test" 2>&1)"
assert_eq "directive text in a pane does not match" "yes" \
  "$([[ "$out" != *MATCH* ]] && echo yes || echo no)"
assert_eq "directive text in a pane sends nothing" "" "$(sends)"

echo "== false-positive guards =="
reset_state
out="$(FAKE_CAPTURE="$T/benign.txt" "$T/tmux-resume-test" 2>&1)"
assert_eq "benign pane does not match" "yes" \
  "$([[ "$out" != *MATCH* ]] && echo yes || echo no)"
assert_eq "benign pane receives nothing" "" "$(sends)"

echo "== resolved prefix is published for the shell helpers =="
# tauto/tnoauto/tautols read the prefix from here instead of hard-coding `auto-`.
# If this stops agreeing with the gate, those helpers rename windows to a prefix
# the gate rejects and silently leave the pane opted out.
assert_eq "prints the file directive's prefix" "auto-" \
  "$("$T/tmux-resume-test" --print-optin-prefix)"
assert_eq "prints the env override" "ci-" \
  "$(TMUX_RESUME_OPTIN_PREFIX="ci-" "$T/tmux-resume-test" --print-optin-prefix)"
assert_eq "prints nothing when sending is disabled" "" \
  "$(TMUX_RESUME_OPTIN_PREFIX="" "$T/tmux-resume-test" --print-optin-prefix)"
assert_eq "printing the prefix sends nothing" "" "$(sends)"

echo "== unwritable marker holds, and says why =="
# resume_pane runs under `|| true`, so a failed marker write does not trip
# errexit. Without the explicit check, held stays -1 and every hourly scan
# reports a fresh "first sighting" — holding forever while claiming otherwise.
reset_state
: > "$T/blocked"   # a regular file, so mkdir -p "$T/blocked/state" cannot succeed
out="$(TMUX_RESUME_STATE_DIR="$T/blocked/state" run)"
assert_eq "unwritable marker names the cause" "yes" \
  "$([[ "$out" == *"marker unwritable"* ]] && echo yes || echo no)"
assert_eq "unwritable marker does not claim a first sighting" "yes" \
  "$([[ "$out" != *"first sighting"* ]] && echo yes || echo no)"
assert_eq "unwritable marker still sends nothing" "" "$(sends)"

echo
if [[ $FAILED -eq 0 ]]; then echo "all checks passed"; else echo "FAILURES — see above"; fi
exit $FAILED
