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
SETUP_SH="$REPO/scripts/cloud/setup.sh"
ZSHRC="$REPO/config/zshrc.sh"
CONTEXT_HOOK="$REPO/claude/hooks/context_auto_apply.sh"

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

# Both are invoked directly below. A missing exec bit on the user-data script
# also makes `hz create` die at its -x check *before* the confirmation line,
# which fails the argv assertions with a completely unrelated message.
check_eq "hz is executable" "yes" "$([[ -x "$HZ" ]] && echo yes || echo no)"
check_eq "hetzner-user-data.sh is executable" "yes" \
    "$([[ -x "$USER_DATA" ]] && echo yes || echo no)"

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

# The documented default and the coded default drifted apart once already:
# --help said cx23 while cmd_create defaulted to cpx42, and `hz up` inherits that
# default *and* implies --yes — so the mismatch billed for a much larger server
# with no confirmation step. Assert both the literal and the doc/code parity: the
# parity check alone passes vacuously when the help line stops matching.
doc_type="$(awk '$1=="--type"{print $2; exit}' <<<"$out")"
check_eq "--help documents a --type default" "cx23" "$doc_type"
create_type="$("$HZ" create testbox 2>&1 | sed -n 's/.* type=\([^ ]*\).*/\1/p')"
check_eq "create's default --type is cx23" "cx23" "$create_type"
check_eq "documented --type default matches the coded one" "$doc_type" "$create_type"

# ── tmux limits for detached agent runs ────────────────────────────────────

section "config/tmux.conf"

check_eq "history-limit is raised for long agent output" \
    "set-option -g history-limit 100000" \
    "$(grep -E '^set-option -g history-limit' "$TMUX_CONF")"

check_eq "destroy-unattached is pinned off" \
    "set-option -g destroy-unattached off" \
    "$(grep -E '^set-option -g destroy-unattached' "$TMUX_CONF")"

# tmux applies the last matching line, so an older `set -g history-limit 10000`
# surviving below the new one would win while the assertion above still passes.
check_eq "history-limit is set exactly once" "1" \
    "$(grep -cE '^[[:space:]]*set(-option)?[[:space:]].*history-limit' "$TMUX_CONF")"
check_eq "destroy-unattached is set exactly once" "1" \
    "$(grep -cE '^[[:space:]]*set(-option)?[[:space:]].*destroy-unattached' "$TMUX_CONF")"

# ── idle-shutdown activity check ───────────────────────────────────────────

section "idle-shutdown activity check"

# Extract the checker exactly as `install` writes it, and exercise --explain,
# which reports each signal and never powers anything off.
CHECK="$FIXTURE/idle-shutdown-check"
# Installer indents the heredoc opener; match without ^ so extraction works.
awk '/cat > \/usr\/local\/sbin\/idle-shutdown-check <</{f=1; next} /^CHECK$/{f=0} f' \
    "$IDLE_INSTALLER" > "$CHECK"
chmod +x "$CHECK"

# A truncated extraction is still non-empty, so assert the shebang rather than
# just the size — otherwise a broken extraction reads as "checker missing".
check_eq "extracted the embedded checker" "#!/bin/bash" "$(head -1 "$CHECK")"

if [[ "$(head -1 "$CHECK")" != "#!/bin/bash" ]]; then
    printf '  skip idle-checker behaviour tests (extraction failed)\n'
elif ! command -v tmux >/dev/null 2>&1; then
    printf '  skip tmux behaviour tests (tmux not installed)\n'
else
    export TMUX_TMPDIR="$FIXTURE"

    # An empty session — a leftover from `cw` or tmux-resurrect. This must NOT
    # count as activity, or the watchdog never fires and the box bills forever.
    tmux -L idletest new -d -s leftover bash 2>/dev/null
    sleep 1
    # Without this gate the assertion below passes vacuously: a tmux that failed
    # to start also reports "none".
    if tmux -L idletest has-session -t leftover 2>/dev/null; then
        pass "fixture: idle tmux session started"
        out="$(TMUX_TMPDIR="$FIXTURE" "$CHECK" --explain 2>/dev/null)"
        check_contains "a bare-shell tmux session is not activity" \
            "tmux work:      none" "$out"
    else
        lose "fixture: idle tmux session started" "session 'leftover'" "tmux new failed"
    fi
    tmux -L idletest kill-server 2>/dev/null

    # A detached session actually running something — the case that used to get
    # powered off mid-run.
    tmux -L worktest new -d -s working 'sleep 300' 2>/dev/null
    sleep 1
    if tmux -L worktest has-session -t working 2>/dev/null; then
        pass "fixture: working tmux session started"
        out="$(TMUX_TMPDIR="$FIXTURE" "$CHECK" --explain 2>/dev/null)"
        # Only the `tmux work:` line is asserted, never the verdict: active()
        # short-circuits on tty_recently_active, and whoever runs this suite is
        # logged in with a fresh tty mtime — so the verdict reads ACTIVE either
        # way and cannot discriminate the signal under test.
        check_contains "a detached tmux session running work IS activity" \
            "tmux work:      running" "$out"
    else
        lose "fixture: working tmux session started" "session 'working'" "tmux new failed"
    fi
    tmux -L worktest kill-server 2>/dev/null

    unset TMUX_TMPDIR
fi

# ── checkout location / DOT_DIR resolution ─────────────────────────────────

section "DOT_DIR resolution"

# The decision is to keep the checkout at ~/code/dotfiles (see the comment at the
# DOTFILES= assignment in scripts/cloud/setup.sh). These assertions pin the two
# things that make that decision safe rather than merely inherited: DOT_DIR is
# actually exported, and the consumers that matter can find the checkout without
# it. A silent move of the clone path would otherwise only surface on a real box.

check_eq "setup.sh clones to code/dotfiles" \
    'DOTFILES="$USER_HOME/code/dotfiles"' \
    "$(grep -E '^DOTFILES=' "$SETUP_SH")"

# The zsh branch of deploy.sh is the one that actually runs; its bash branch has
# always exported DOT_DIR. A bare assignment here is what made every non-login
# consumer fall through to a hardcoded literal.
check_eq "config/zshrc.sh exports DOT_DIR" \
    "export DOT_DIR=\${CONFIG_DIR:h}" \
    "$(grep -E '^(export )?DOT_DIR=' "$ZSHRC")"
check_eq "DOT_DIR is assigned exactly once in config/zshrc.sh" "1" \
    "$(grep -cE '^(export )?DOT_DIR=' "$ZSHRC")"

# Extract the hook's resolver and run it against a fixture that mimics the
# deployed layout (~/.claude -> $DOT_DIR/claude), so the assertion is about
# behaviour rather than about the text of the block.
RESOLVER="$FIXTURE/resolver-block.sh"
awk '/^# BEGIN dot-dir-resolver$/{f=1; next} /^# END dot-dir-resolver$/{f=0} f' \
    "$CONTEXT_HOOK" > "$RESOLVER"
check_contains "extracted the DOT_DIR resolver" "BASH_SOURCE" "$(cat "$RESOLVER")"

if ! grep -q 'BASH_SOURCE' "$RESOLVER"; then
    printf '  skip resolver behaviour tests (extraction failed)\n'
else
    FAKE="$FIXTURE/fake-dotfiles"
    mkdir -p "$FAKE/config" "$FAKE/claude/hooks"
    { printf '#!/usr/bin/env bash\n'; cat "$RESOLVER"; printf 'printf "%%s\\n" "$DOT_DIR"\n'; } \
        > "$FAKE/claude/hooks/probe.sh"
    ln -s "$FAKE/claude" "$FIXTURE/dot-claude"

    # Reached through the symlink, exactly as Claude Code invokes ~/.claude/hooks/*.
    check_eq "resolver finds the checkout through the ~/.claude symlink" \
        "$(realpath "$FAKE")" \
        "$(env -u DOT_DIR bash "$FIXTURE/dot-claude/hooks/probe.sh" 2>/dev/null)"

    # An inherited DOT_DIR must win: the resolver is a fallback, not an override.
    check_eq "resolver leaves an inherited DOT_DIR alone" \
        "/sentinel/dot/dir" \
        "$(DOT_DIR=/sentinel/dot/dir bash "$FIXTURE/dot-claude/hooks/probe.sh" 2>/dev/null)"

    # Exactly one hardcoded path may remain — the last-resort branch taken only
    # when realpath is unavailable. More than one means the resolver regressed
    # back into naming the checkout location.
    check_eq "resolver keeps exactly one hardcoded fallback" "1" \
        "$(grep -c 'code/dotfiles' "$RESOLVER")"
fi

# ── summary ────────────────────────────────────────────────────────────────

printf '\n%s\n' "-------------------------"
printf 'passed: %d   failed: %d\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
