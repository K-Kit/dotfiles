#!/usr/bin/env zsh
# Runs custom_bins/clear-mac-apps end to end with osascript stubbed, the way
# tests/test_hide_idle_apps.zsh does. Same reasoning: System Events needs a
# window server, which CI and sandboxes don't have.
#
# The scripts under test are HARDLINKED into the fake tree, never copied: they
# resolve config and helper paths from ${0:A:h}, and :A resolves symlinks back
# to the real repo. A hardlink is the same inode, so what runs here cannot
# drift from what ships.
set -uo pipefail

REPO="${0:A:h:h}"
# Under the repo's gitignored tmp/, not $TMPDIR: agent sandboxes commonly allow
# only one level below their temp root, and this tree is several deep.
mkdir -p "$REPO/tmp"
WORK="$(mktemp -d "$REPO/tmp/clear-mac-test.XXXXXX")"
ROOT="$WORK/root"
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
check_eq() {  # check_eq <label> <actual> <expected>
    if [[ "$2" == "$3" ]]; then
        print -r -- "  ok   $1"; (( PASS++ ))
    else
        print -r -- "  FAIL $1"; (( FAIL++ ))
        # Real files, not process substitution: /dev/fd is not readable under
        # the agent sandbox and diff would fail instead of showing the diff.
        print -r -- "$3" > "$WORK/expected"; print -r -- "$2" > "$WORK/actual"
        diff "$WORK/expected" "$WORK/actual" | sed 's/^/       /'
    fi
}

# --- fake tree -------------------------------------------------------------
mkdir -p "$ROOT/custom_bins" "$ROOT/config" "$ROOT/bin"
ln "$REPO/custom_bins/clear-mac-apps"       "$ROOT/custom_bins/clear-mac-apps"
ln "$REPO/custom_bins/app-lifecycle-config" "$ROOT/custom_bins/app-lifecycle-config"
CMA="$ROOT/custom_bins/clear-mac-apps"

# Stands in for osascript. The app list and Chrome's tabs both arrive here; the
# Chrome query comes in on stdin (heredoc), everything else as arguments.
cat > "$ROOT/bin/osascript" <<'STUB'
#!/usr/bin/env zsh
script="$*"
[[ -z "$script" ]] && script="$(cat)"
# The close paths come first: their scripts name the process too, so a Chrome
# close would otherwise be answered with Chrome's tab list.
case "$script" in
    *AXCloseButton*)
        [[ -n "${STUB_AX_LOG:-}" ]] && print -r -- "$script" >> "$STUB_AX_LOG"
        (( ${STUB_AX_RC:-0} != 0 )) && exit "${STUB_AX_RC}"
        print -r -- 0 ;;
    *keystroke*)
        [[ -n "${STUB_KEY_LOG:-}" ]] && print -r -- "$script" >> "$STUB_KEY_LOG" ;;
    *"to count windows"*)         print -r -- "${STUB_WINDOW_COUNT:-2}" ;;
    *"background only is false"*) print -rn -- "${STUB_APP_LIST:-}" ;;
    *"Google Chrome"*)
        # Chrome's tabs are read twice in a real run: once to classify it, and
        # again inside close_app_selectively. STUB_CHROME_TABS_2 makes the
        # protected tab vanish between the two - the exact race that decides
        # whether a capped run can still reach a quit.
        if [[ -n "${STUB_CHROME_CALLS:-}" ]]; then
            print -rn -- x >> "$STUB_CHROME_CALLS"
            if [[ -n "${STUB_CHROME_TABS_2:-}" && "$(cat "$STUB_CHROME_CALLS")" != x ]]; then
                print -rn -- "$STUB_CHROME_TABS_2"
                exit 0
            fi
        fi
        print -rn -- "${STUB_CHROME_TABS:-}" ;;
    *)                            : ;;   # window titles: nothing to report
esac
STUB
chmod +x "$ROOT/bin/osascript"

if [[ "$CMA" -ef "$REPO/custom_bins/clear-mac-apps" ]]; then
    print -r -- "  ok   script under test is the real file (same inode)"; (( PASS++ ))
else
    print -r -- "  FAIL script under test is a copy, not the real file"; (( FAIL++ ))
fi

# --- canned inputs ---------------------------------------------------------
# One app per bucket the old config produced, plus one it never mentioned.
export STUB_APP_LIST="Ghostty|com.mitchellh.ghostty
Bear|net.shinyfrog.bear
Mouseless|com.sinusoid.mouseless
Spark Desktop|com.readdle.smartemail
Safari|com.apple.Safari
Google Chrome|com.google.Chrome
"
export STUB_CHROME_TABS="1|Inbox (3) - Gmail
2|Google Meet - standup
"

AXCAP="$ROOT/home/.cache/hide-idle-apps/ax-capability"

run() {  # run [args...]; sets OUT / ERR / RC
    [[ -n "${STUB_AX_LOG:-}" ]]     && : > "$STUB_AX_LOG"
    [[ -n "${STUB_KEY_LOG:-}" ]]    && : > "$STUB_KEY_LOG"
    [[ -n "${STUB_CHROME_CALLS:-}" ]] && : > "$STUB_CHROME_CALLS"
    # HOME is faked because the close path remembers which apps cannot be closed
    # by clicking, under ~/.cache - a real run must not write to the real one.
    OUT=$(PATH="$ROOT/bin:$PATH" HOME="$ROOT/home" "$CMA" "$@" 2>"$WORK/err")
    RC=$?
    # cat, not $(<file): zsh evaluates the read-file form even under `zsh -n`,
    # where these variables are unset, so a syntax check would print errors.
    ERR=$(cat "$WORK/err" 2>/dev/null)
    # Surface stderr from any run that failed. Without this a script that dies
    # early just produces empty output and every later check fails for reasons
    # the log never explains.
    (( RC == 0 )) || print -r -- "  note (rc=$RC): $ERR"
}

# --- 1. migration preserves behaviour exactly ------------------------------
# The golden file was captured from the LAST PRE-MIGRATION clear-mac-apps run
# against this same stubbed app list. Migrating the old config and running the
# new script must reproduce it byte for byte - that is the whole claim the
# migration makes, and the only way to be sure the config swap changed nothing.
print -r -- "1. migrating the old config changes no behaviour"
"$CMA" --migrate-config "$REPO/tests/fixtures/clear_mac_apps.conf" \
    > "$ROOT/config/app-lifecycle.yaml" 2>"$WORK/err"
# On RC, not on empty stderr: every string contains the empty string, so a
# `check ... ""` would pass no matter what happened.
check "migration succeeds" "$?" "0"
run --dry-run
check_eq "dry run is byte-identical to pre-migration" \
         "$OUT" "$(cat "$REPO/tests/fixtures/clear_mac_apps.dry-run.txt")"

# Criteria 3-6 hold against the migrated config, where Bear is still close and
# Ghostty still skip - so they test the flags, not the later hand edits.
# --- 3. --only narrows to one app ------------------------------------------
print -r -- "3. --only reports just that app"
run --only Bear --dry-run
check     "Bear still closes its windows"  "$OUT" "Would CLOSE WINDOWS (1):
  - Bear"
check_not "no other app is reported"       "$OUT" "- Safari"
check     "quit bucket is empty"           "$OUT" "Would QUIT (0):"

# --- 4. --only cannot override the config ----------------------------------
print -r -- "4. --only on a skipped app still skips it"
run --only Ghostty --dry-run
check     "Ghostty lands in no-touch"      "$OUT" "Would SKIP (no-touch):
  - Ghostty"
check     "nothing would be quit"          "$OUT" "Would QUIT (0):"
check     "nothing would be closed"        "$OUT" "Would CLOSE WINDOWS (0):"

# --- 5. a protected tab downgrades quit to selective-close -----------------
print -r -- "5. a Google Meet tab protects Chrome"
run --dry-run
check     "Chrome is selective-closed"     "$OUT" "Would SELECTIVE-CLOSE"
check     "and named"                      "$OUT" "- Google Chrome"
check_not "Chrome is never quit outright"  "$OUT" "Would QUIT (2):"

# --- 6. an app the config never mentions is quit ---------------------------
print -r -- "6. an unlisted app takes the quit default"
run --dry-run
check     "Safari is quit"                 "$OUT" "Would QUIT (1):
  - Safari"

# --- max-action caps the rung ----------------------------------------------
# Not in the spec's criteria, but load-bearing: the idle job reaches its close
# rung through this flag, and without the cap an app whose `manual:` is quit
# would be quit there, collapsing hide -> close -> quit into one step.
print -r -- "7. --max-action close downgrades quits, promotes nothing"
run --max-action close --dry-run
check     "nothing is quit"                "$OUT" "Would QUIT (0):"
check     "the quit app closes instead"    "$OUT" "- Safari"
check     "slow-quit is capped too"        "$OUT" "Would SLOW-QUIT (0):"
check     "skipped app stays skipped"      "$OUT" "Would SKIP (no-touch):
  - Ghostty"

# --- 2. the shipped config, after the deliberate moves ---------------------
print -r -- "2. the shipped config quits what was moved off close-windows"
cp "$REPO/config/app-lifecycle.yaml" "$ROOT/config/app-lifecycle.yaml"
export STUB_APP_LIST="Claude|com.anthropic.claudefordesktop
Granola|so.granola.app
Tailscale|io.tailscale.ipn.macsys
NordVPN|com.nordvpn.macos
Bear|net.shinyfrog.bear
Spotify|com.spotify.client
"
run --dry-run
check     "Claude is quit"                 "$OUT" "- Claude"
check     "Granola is quit"                "$OUT" "- Granola"
check     "five apps quit, none closed"    "$OUT" "Would QUIT (4):"
check     "Bear still only closes"         "$OUT" "Would CLOSE WINDOWS (2):"
check     "Spotify still only closes"      "$OUT" "- Spotify"

# --- the close path itself, not dry-run ------------------------------------
# Everything above stops at --dry-run, so none of it ever reaches the code that
# actually closes a window. These four run it for real against the stub. Bear is
# close-windows in the shipped config, so --only Bear reaches exactly that path
# and nothing else - no quits to wait on.
export STUB_AX_LOG="$WORK/ax.log" STUB_KEY_LOG="$WORK/key.log"

print -r -- "9. closing windows clicks the close button rather than typing"
export STUB_WINDOW_COUNT=2 STUB_AX_RC=0
run --only Bear
check    "Bear's windows are closed"        "$OUT" "Closing windows: Bear"
check    "by clicking the close button"     "$(cat "$STUB_AX_LOG" 2>/dev/null)" "AXCloseButton"
check_eq "no keystroke, so no focus stolen" "$(cat "$STUB_KEY_LOG" 2>/dev/null)" ""
check_eq "and nothing is remembered"        "$(cat "$AXCAP" 2>/dev/null)" ""

print -r -- "10. an app with no close button falls back to keystrokes"
export STUB_AX_RC=1
run --only Bear
check "the click is attempted first" "$(cat "$STUB_AX_LOG" 2>/dev/null)"  "AXCloseButton"
check "keystrokes take over"         "$(cat "$STUB_KEY_LOG" 2>/dev/null)" "keystroke"
check "and the app is remembered"    "$(cat "$AXCAP" 2>/dev/null)"        "Bear"

print -r -- "11. a remembered app is never probed again"
run --only Bear
check_eq "no second probe"        "$(cat "$STUB_AX_LOG" 2>/dev/null)" ""
check    "straight to keystrokes" "$(cat "$STUB_KEY_LOG" 2>/dev/null)" "keystroke"
check_eq "recorded exactly once"  "$(grep -c '^Bear$' "$AXCAP")" "1"

# Criterion 20: a probe that fails for want of a window says nothing about
# whether the app supports clicking, so a windowless app must not be probed at
# all - and above all must not be branded keystroke-only on that evidence.
print -r -- "12. an app with no windows is left alone, and teaches us nothing"
export STUB_WINDOW_COUNT=0
run --only Spotify
check_eq  "no probe"                 "$(cat "$STUB_AX_LOG" 2>/dev/null)"  ""
check_eq  "no keystroke"             "$(cat "$STUB_KEY_LOG" 2>/dev/null)" ""
check_not "not recorded"             "$(cat "$AXCAP" 2>/dev/null)"        "Spotify"

# --- every value on the scale gets dispatched ------------------------------
# No app in the shipped config asks for `manual: hide`, and none pairs `close`
# with `slow`, so nothing above would notice either going wrong. Both did: with
# a bucket per value missing, `hide` fell through to the `quit` default, and
# `slow` - which only says how a quit is AWAITED - was checked as though it were
# a bucket of its own and outranked `close`. Either way the config asked for a
# gentler action and got the harshest one.
print -r -- "13. a hide-only app is hidden, not quit"
print -r -- 'defaults:
  manual: quit
  auto: quit
apps:
  Bear:          {manual: hide}
  Spark Desktop: {manual: close, slow: true}' > "$ROOT/config/app-lifecycle.yaml"
export STUB_APP_LIST="Bear|net.shinyfrog.bear
Spark Desktop|com.readdle.smartemail
Safari|com.apple.Safari
"
run --dry-run
check     "Bear is hidden"                 "$OUT" "Would HIDE (1):
  - Bear"
check_not "and never quit"                 "$OUT" "- Bear
  - Safari"
check     "the default app still quits"    "$OUT" "Would QUIT (1):
  - Safari"

print -r -- "14. slow modifies a quit, it does not create one"
check     "the close app still closes"     "$OUT" "Would CLOSE WINDOWS (1):
  - Spark Desktop"
check     "and is not slow-quit"           "$OUT" "Would SLOW-QUIT (0):"

# Codex P2: the cap was applied when the buckets were built, so an app already
# sorted into selective-close carried no memory of it. If the protected tab is
# gone by the time close_app_selectively re-checks, that function's "nothing
# worth keeping -> quit the app" branch fired and the idle job's CLOSE rung
# performed a quit.
print -r -- "15. --max-action close survives a protected tab vanishing mid-run"
cp "$REPO/config/app-lifecycle.yaml" "$ROOT/config/app-lifecycle.yaml"
export STUB_APP_LIST="Google Chrome|com.google.Chrome
"
export STUB_CHROME_CALLS="$WORK/chrome.calls"
export STUB_CHROME_TABS_2="1|Inbox (3) - Gmail
"
run --only "Google Chrome" --max-action close
check     "the quit is refused"            "$OUT" "capped at close"
check_not "and Chrome is not quit"         "$OUT" "Quitting Google Chrome"
unset STUB_CHROME_TABS_2 STUB_CHROME_CALLS

# --- a broken config stops us dead -----------------------------------------
# The default action here is "quit", so a config we cannot read must abort
# rather than fall back to defaults and quit everything.
print -r -- "8. an unreadable config aborts instead of guessing"
print -r -- "defaults:"$'\n'"  close_after: abc" > "$ROOT/config/app-lifecycle.yaml"
run --dry-run
check     "exits non-zero"                 "$(( RC != 0 ))" "1"
check     "names the offending key"        "$ERR" "close_after"
check_not "acts on nothing"                "$OUT" "Would QUIT"

print -r -- ""
print -r -- "PASS=$PASS FAIL=$FAIL"
(( FAIL == 0 )) || exit 1
