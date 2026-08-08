#!/usr/bin/env bash
# Tests for the Hetzner provisioning flow: user-data rendering, hz argument
# handling, the idle-shutdown activity check, and the tmux limits that a
# detached agent run depends on.
#
# Hermetic: no server is created, no hcloud call is made, no network is
# touched. `hz create` without --yes stops at the confirmation line, which is
# where the argv assertions live; anything past that needs hcloud and is out of
# scope here.
#
# Assertions compare full strings against literals rather than checking that a
# flag is "present". A presence check passes when a flag keeps its name and
# silently changes its operand — the failure this repo already hit once with
# `--tools ""` -> `--tools default` (CLAUDE.md Learnings, 2026-08-06).
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HZ="$REPO/custom_bins/hz"
USER_DATA="$REPO/scripts/cloud/hetzner-user-data.sh"
TEMPLATE="$REPO/scripts/cloud/hetzner-cloud-init.yaml"
IDLE_INSTALLER="$REPO/scripts/cloud/idle-shutdown-install.sh"
TMUX_CONF="$REPO/config/tmux.conf"

PASS=0
FAIL=0

TMP_ROOT="${TMPDIR:-/tmp}"
FIXTURE=$(mktemp -d "${TMP_ROOT%/}/cloud-test.XXXXXX") || {
    echo "cannot create fixture dir" >&2; exit 1
}
trap 'rm -rf "$FIXTURE"' EXIT

pass() { PASS=$((PASS + 1)); printf '  ok   %s\n' "$1"; }
lose() {
    FAIL=$((FAIL + 1))
    printf '  FAIL %s\n' "$1"
    [[ $# -gt 1 ]] && printf '       expected: %s\n' "$2"
    [[ $# -gt 2 ]] && printf '       actual:   %s\n' "$3"
}

check_eq() { # label expected actual
    if [[ "$2" == "$3" ]]; then pass "$1"; else lose "$1" "$2" "$3"; fi
}

check_contains() { # label needle haystack
    if [[ "$3" == *"$2"* ]]; then pass "$1"; else lose "$1" "contains: $2" "$3"; fi
}

check_missing() { # label needle haystack
    if [[ "$3" != *"$2"* ]]; then pass "$1"; else lose "$1" "must NOT contain: $2" "$3"; fi
}

section() { printf '\n== %s ==\n' "$1"; }

# ── user-data rendering ────────────────────────────────────────────────────

section "hetzner-user-data.sh"

out="$("$USER_DATA" --no-secrets 2>/dev/null)"
check_eq "--no-secrets emits the template verbatim" \
    "$(cat "$TEMPLATE")" "$out"

# The branch is the one identity field hz has to be able to override, and it
# is interpolated into a raw.githubusercontent URL on the box.
out="$("$USER_DATA" --no-secrets --branch wip-feature 2>/dev/null)"
check_contains "--branch rewrites DOTFILES_BRANCH" \
    "DOTFILES_BRANCH=wip-feature" "$out"
check_missing "--branch leaves no stale DOTFILES_BRANCH=main" \
    "DOTFILES_BRANCH=main" "$out"
check_eq "--branch rewrites exactly one line" \
    "1" "$(grep -c 'DOTFILES_BRANCH=' <<<"$out")"
check_eq "--branch preserves the template's indentation" \
    "      DOTFILES_BRANCH=wip-feature" \
    "$(grep 'DOTFILES_BRANCH=' <<<"$out")"

check_eq "default branch is main" \
    "      DOTFILES_BRANCH=main" \
    "$("$USER_DATA" --no-secrets 2>/dev/null | grep 'DOTFILES_BRANCH=')"

"$USER_DATA" --no-secrets --branch 'main;curl evil' >/dev/null 2>&1
check_eq "a branch name with shell metacharacters is rejected" "1" "$?"

"$USER_DATA" --no-secrets --nonsense >/dev/null 2>&1
check_eq "an unknown option is rejected" "1" "$?"

# A PAT must never reach user-data: Hetzner's metadata service serves it to
# every local user on the box.
check_missing "no GITHUB_TOKEN in rendered user-data" \
    "GITHUB_TOKEN" "$("$USER_DATA" --no-secrets 2>/dev/null)"

# ── cloud-init template shape ──────────────────────────────────────────────

section "hetzner-cloud-init.yaml"

# hz appends its idle-shutdown step by string-appending a list item to the end
# of the file. That is only correct while runcmd: is the last top-level key.
last_key="$(grep -E '^[a-z_]+:' "$TEMPLATE" | tail -1)"
check_eq "runcmd: is the last top-level key (hz appends to it)" \
    "runcmd:" "$last_key"

check_contains "bootstrap retries transient network failures" \
    "--retry 5" "$(cat "$TEMPLATE")"
check_contains "bootstrap records a machine-readable verdict" \
    "/var/lib/dotfiles-bootstrap.status" "$(cat "$TEMPLATE")"

# ── hz argument handling ───────────────────────────────────────────────────

section "hz"

# Without --yes, create stops at the confirmation line — no hcloud needed.
out="$("$HZ" create testbox 2>&1)"
check_contains "create without --yes refuses" "refusing to create a paid server" "$out"
check_contains "create defaults to branch=main" "branch=main" "$out"
check_contains "create waits by default (900s)" "wait=900s" "$out"
check_contains "create leaves github-auth off by default" "github-auth=off" "$out"
check_contains "create leaves idle-shutdown off by default" "idle-shutdown=off" "$out"

out="$("$HZ" create testbox --branch wip --timeout 120 --github-auth --idle-hours 6 2>&1)"
check_contains "--branch reaches the confirmation line" "branch=wip" "$out"
check_contains "--timeout reaches the confirmation line" "wait=120s" "$out"
check_contains "--github-auth reaches the confirmation line" "github-auth=on" "$out"
check_contains "--idle-hours implies idle-shutdown" "idle-shutdown=6h" "$out"

out="$("$HZ" create testbox --no-wait 2>&1)"
check_contains "--no-wait reaches the confirmation line" "wait=off" "$out"

out="$("$HZ" create testbox --timeout 0 --yes 2>&1)"
check_contains "--timeout 0 is rejected" "positive integer" "$out"

out="$("$HZ" wait 2>&1)"
check_contains "wait without a server name shows usage" "usage: hz wait" "$out"

out="$("$HZ" --help 2>&1)"
check_contains "wait is documented in --help" "hz wait <server>" "$out"
check_contains "--github-auth documents where the token comes from" \
    "fnox get GITHUB_TOKEN" "$out"

# ── tmux limits for detached agent runs ────────────────────────────────────

section "config/tmux.conf"

check_eq "history-limit is raised for long agent output" \
    "set-option -g history-limit 100000" \
    "$(grep -E '^set-option -g history-limit' "$TMUX_CONF")"

check_eq "destroy-unattached is pinned off" \
    "set-option -g destroy-unattached off" \
    "$(grep -E '^set-option -g destroy-unattached' "$TMUX_CONF")"

# ── idle-shutdown activity check ───────────────────────────────────────────

section "idle-shutdown activity check"

# Extract the checker exactly as `install` writes it, and exercise --explain,
# which reports each signal and never powers anything off.
CHECK="$FIXTURE/idle-shutdown-check"
# Installer indents the heredoc opener; match without ^ so extraction works.
awk '/cat > \/usr\/local\/sbin\/idle-shutdown-check <</{f=1; next} /^CHECK$/{f=0} f' \
    "$IDLE_INSTALLER" > "$CHECK"
chmod +x "$CHECK"

if [[ ! -s "$CHECK" ]]; then
    lose "extracted the embedded checker" "non-empty script" "empty"
else
    pass "extracted the embedded checker"

    if ! command -v tmux >/dev/null 2>&1; then
        printf '  skip tmux behaviour tests (tmux not installed)\n'
    else
        export TMUX_TMPDIR="$FIXTURE"

        # An empty session — a leftover from `cw` or tmux-resurrect. This must
        # NOT count as activity, or the watchdog never fires and the box bills
        # forever.
        tmux -L idletest new -d -s leftover bash 2>/dev/null
        sleep 1
        out="$(TMUX_TMPDIR="$FIXTURE" "$CHECK" --explain 2>/dev/null)"
        check_contains "a bare-shell tmux session is not activity" "tmux work:      none" "$out"
        tmux -L idletest kill-server 2>/dev/null

        # A detached session actually running something — the case that used to
        # get powered off mid-run.
        tmux -L worktest new -d -s working 'sleep 300' 2>/dev/null
        sleep 1
        out="$(TMUX_TMPDIR="$FIXTURE" "$CHECK" --explain 2>/dev/null)"
        check_contains "a detached tmux session running work IS activity" \
            "tmux work:      running" "$out"
        check_contains "running work makes the verdict ACTIVE" \
            "verdict:        ACTIVE" "$out"
        tmux -L worktest kill-server 2>/dev/null

        unset TMUX_TMPDIR
    fi
fi

# ── summary ────────────────────────────────────────────────────────────────

printf '\n%s\n' "-------------------------"
printf 'passed: %d   failed: %d\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
