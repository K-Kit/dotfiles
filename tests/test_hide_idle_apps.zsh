#!/usr/bin/env zsh
# Runs custom_bins/hide-idle-apps end to end with its two external dependencies
# stubbed: osascript (System Events) and tools/window-exposure. Both need a
# window server, which CI and sandboxes don't have - stubbing them is what lets
# the decision logic be tested at all rather than by eye on a real desktop.
#
# The script under test is HARDLINKED into the fake tree, never copied: it
# resolves its own config and helper paths from ${0:A:h}, and :A resolves
# symlinks back to the real repo. A hardlink is the same inode, so what runs
# here cannot drift from what ships.
set -uo pipefail

REPO="${0:A:h:h}"
# Under the repo's gitignored tmp/, not $TMPDIR: agent sandboxes commonly allow
# only one level below their temp root, and this tree is several deep.
mkdir -p "$REPO/tmp"
WORK="$(mktemp -d "$REPO/tmp/hide-idle-test.XXXXXX")"
ROOT="$WORK/root"
FAKEHOME="$WORK/home"
trap 'rm -rf "$WORK"' EXIT   # only the temp tree this script just created
PASS=0 FAIL=0

check() {  # check <label> <haystack> <needle>
    if [[ "$2" == *"$3"* ]]; then
        print -r -- "  ok   $1"; (( PASS++ ))
    else
        print -r -- "  FAIL $1 -- expected to find: $3"; (( FAIL++ ))
    fi
}
check_not() {
    if [[ "$2" != *"$3"* ]]; then
        print -r -- "  ok   $1"; (( PASS++ ))
    else
        print -r -- "  FAIL $1 -- should NOT contain: $3"; (( FAIL++ ))
    fi
}

# --- fake tree -------------------------------------------------------------
mkdir -p "$ROOT/custom_bins" "$ROOT/config" "$ROOT/tools/window-exposure" \
         "$ROOT/bin" "$FAKEHOME"
ln "$REPO/custom_bins/hide-idle-apps" "$ROOT/custom_bins/hide-idle-apps"
cp "$REPO/config/clear_mac_apps.conf" "$REPO/config/hide-idle.conf" "$ROOT/config/"

# Stands in for osascript: returns a canned System Events snapshot, and records
# hide attempts instead of performing them.
cat > "$ROOT/bin/osascript" <<'STUB'
#!/usr/bin/env zsh
if [[ "$*" == *"set visible"* ]]; then
    print -r -- "$*" >> "${STUB_HIDE_LOG:?}"
    exit 0
fi
print -rn -- "${STUB_SNAPSHOT:-}"
STUB

# Stands in for the exposure helper: records its flags, then emits canned output
# or an exit code the caller picks.
cat > "$ROOT/tools/window-exposure/window-exposure" <<'STUB'
#!/usr/bin/env zsh
print -r -- "$*" >> "${STUB_HELPER_ARGS_LOG:?}"
(( ${STUB_HELPER_RC:-0} != 0 )) && exit "${STUB_HELPER_RC}"
print -rn -- "${STUB_HELPER_OUT:-}"
STUB

print -r -- "// stub" > "$ROOT/tools/window-exposure/main.swift"
chmod +x "$ROOT/bin/osascript" "$ROOT/tools/window-exposure/window-exposure"
touch "$ROOT/tools/window-exposure/window-exposure"  # must out-date main.swift

if [[ "$ROOT/custom_bins/hide-idle-apps" -ef "$REPO/custom_bins/hide-idle-apps" ]]; then
    print -r -- "  ok   script under test is the real file (same inode)"; (( PASS++ ))
else
    print -r -- "  FAIL script under test is a copy, not the real file"; (( FAIL++ ))
fi

# --- canned inputs ---------------------------------------------------------
# 101 Safari frontmost; 102 Telegram 35% visible (under the 40% threshold, so
# covered); 103 Cursor 0% and already hidden; 104 Bear 41% (over, so exposed);
# 105 Slack owns no window in the list at all (another Space) -> unknown;
# 106 Ghostty is fully covered and would otherwise qualify, but is excluded.
export STUB_SNAPSHOT="FRONT:101
RUN:101	true	Safari
RUN:102	true	Telegram
RUN:103	false	Cursor
RUN:104	true	Bear
RUN:105	true	Slack
RUN:106	true	Ghostty
"
export STUB_HELPER_OUT=$'101\t1\t1.000\n102\t0\t0.352\n103\t0\t0.000\n104\t1\t0.407\n106\t0\t0.000\n'
export STUB_HELPER_ARGS_LOG="$WORK/helper-args.log"
export STUB_HIDE_LOG="$WORK/hide-attempts.log"

run() {  # run <helper_rc> [args...]; sets OUT / ERR / RC / ARGS
    : > "$STUB_HELPER_ARGS_LOG"   # both logs are per-run, so no run can inherit
    : > "$STUB_HIDE_LOG"          # what an earlier one recorded
    STUB_HELPER_RC="$1"; shift
    export STUB_HELPER_RC
    OUT=$(HOME="$FAKEHOME" PATH="$ROOT/bin:$PATH" \
          "$ROOT/custom_bins/hide-idle-apps" "$@" 2>"$WORK/err")
    RC=$?
    # cat, not $(<file): zsh evaluates the read-file form even under `zsh -n`,
    # where these variables are unset, so a syntax check would print errors.
    ERR=$(cat "$WORK/err" 2>/dev/null)
    ARGS=$(cat "$STUB_HELPER_ARGS_LOG" 2>/dev/null)
}

seed_state() {  # seed_state <seconds since last poll>: every app covered an hour
    local gap="$1" now p
    now=$(date +%s)
    mkdir -p "$FAKEHOME/.cache/hide-idle-apps"
    # Tabs via $'\t': print -r does NOT expand escapes, and a literal backslash-t
    # would silently make every app look freshly seen instead of long idle.
    {
        print -r -- "#v2"
        print -r -- "#last_poll"$'\t'"$(( now - gap ))"
        for p in 101 102 103 104 105 106; do
            print -r -- "$p"$'\t'"$(( now - 3600 ))"$'\t'"1"
        done
    } > "$FAKEHOME/.cache/hide-idle-apps/state"
}

# --- 1. dry run reports measured visibility --------------------------------
print -r -- "1. dry run reports measured visibility"
rm -rf "$FAKEHOME/.cache"
run 0 --dry-run
check     "runs clean"                     "$RC"  "0"
check     "passes --min-visible through"   "$ARGS" "--min-visible 40"
check     "asks for --verbose"             "$ARGS" "--verbose"
check     "frontmost labelled"             "$OUT" "frontmost  Safari"
check     "35% for Telegram"               "$OUT" "35%  Telegram"
check     "41% for Bear"                   "$OUT" "41%  Bear"
check     "0% for Cursor"                  "$OUT" "0%  Cursor"
check     "no window here reads unknown"   "$OUT" "unknown  Slack (no window on this Space)"
check_not "excluded app not reported"      "$OUT" "Ghostty"
check     "first sight hides nothing"      "$OUT" "Nothing to hide."

# --- 2. a bad tunable is not a transient failure ---------------------------
print -r -- "2. exit 64 reads as a bad tunable, not a transient failure"
rm -rf "$FAKEHOME/.cache"
run 64 --dry-run
check     "names the tunable on stderr"    "$ERR" "bad HIDE_IDLE_MIN_VISIBLE_PERCENT="
check_not "not misfiled as transient"      "$ERR" "no usable window list"
check     "hides nothing"                  "$OUT" "Nothing would be hidden"

# --- 3. a transient helper failure stays quiet -----------------------------
print -r -- "3. exit 1 stays a silent transient failure"
rm -rf "$FAKEHOME/.cache"
run 1 --dry-run
check     "reported as transient"          "$OUT" "no usable window list"
check_not "stderr stays quiet"             "$ERR" "hide-idle-apps:"

# --- 4. only the covered, still-visible app is hidden ----------------------
print -r -- "4. real run hides only the covered, still-visible app"
rm -rf "$FAKEHOME/.cache"; seed_state 60
run 0
HIDES=$(cat "$STUB_HIDE_LOG")
check     "covered app is hidden"          "$OUT"   "Hid: Telegram"
check     "hidden by pid, not name"        "$HIDES" "unix id is 102"
check_not "frontmost never hidden"         "$HIDES" "unix id is 101"
check_not "already-hidden not re-hidden"   "$HIDES" "unix id is 103"
check_not "exposed app never hidden"       "$HIDES" "unix id is 104"
check_not "unknown app never hidden"       "$HIDES" "unix id is 105"
check_not "excluded app never hidden"      "$HIDES" "unix id is 106"
check_not "scheduled run skips --verbose"  "$ARGS" "--verbose"

# --- 5. skipped polls (sleep) hide nothing ---------------------------------
print -r -- "5. a gap over 3x the poll interval hides nothing"
rm -rf "$FAKEHOME/.cache"; seed_state 1000
run 0
check_not "no hide attempted after a gap"  "$(cat "$STUB_HIDE_LOG")" "unix id"
check_not "nothing reported hidden"        "$OUT" "Hid:"
rm -rf "$FAKEHOME/.cache"; seed_state 1000
run 0 --dry-run
check     "gap named as the reason"        "$OUT" "polls were skipped"

print -r -- ""
print -r -- "PASS=$PASS FAIL=$FAIL"
(( FAIL == 0 )) || exit 1
