#!/usr/bin/env bash
# Functional test for tmux-resume's detect-only cooldown state machine and the
# send-sequence parser. Self-contained: builds its own fixtures under a temp dir.
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
  list-panes)   echo "fake:0.0" ;;
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

echo "== false-positive guards =="
reset_state
out="$(FAKE_CAPTURE="$T/benign.txt" "$T/tmux-resume-test" 2>&1)"
assert_eq "benign pane does not match" "yes" \
  "$([[ "$out" != *MATCH* ]] && echo yes || echo no)"
assert_eq "benign pane receives nothing" "" "$(sends)"

echo
if [[ $FAILED -eq 0 ]]; then echo "all checks passed"; else echo "FAILURES — see above"; fi
exit $FAILED
