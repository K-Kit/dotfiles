#!/usr/bin/env zsh
# Regression tests for the install.sh / deploy.sh interactive component menu.
#
# The bug this pins: helpers.sh passed `--items <file>` to a claude-tools that
# did not understand the flag. The binary ignored it, read no items, and exited 0
# with no output — and the menu then disabled every component and re-enabled
# none, so `./deploy.sh` deployed nothing and reported success.
#
# Selection policy is tested through _apply_component_selection (no pty needed);
# the shell↔binary contract is tested by inspecting the invocation itself.
set -euo pipefail

REPO_ROOT="${0:A:h:h}"
DOT_DIR="$REPO_ROOT"
export DOT_DIR

source "$REPO_ROOT/config.sh"
source "$REPO_ROOT/scripts/shared/helpers.sh"

FAILURES=0
pass() { print "✓ $*" }
fail() { print -u2 "✗ $*"; FAILURES=$((FAILURES + 1)) }

assert_eq() {
    local expected="$1" actual="$2" what="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass "$what"
    else
        fail "$what — expected '$expected', got '$actual'"
    fi
}

# ─── Empty TUI output must NOT deselect everything ────────────────────────────

for empty_case in "" $'\n\n' "   "; do
    _init_component_vars
    _apply_component_selection deploy "$empty_case" shell tmux claude serena >/dev/null
    assert_eq true "${DEPLOY_SHELL}" "empty output keeps default-on component (case ${(qq)empty_case})"
    assert_eq false "${DEPLOY_SERENA}" "empty output keeps default-off component (case ${(qq)empty_case})"
done

# ─── A real selection applies exactly, and only, what was chosen ──────────────

_init_component_vars
_apply_component_selection deploy $'shell\ngit-config' shell tmux git-config claude >/dev/null
assert_eq true "${DEPLOY_SHELL}" "selected component enabled"
assert_eq true "${DEPLOY_GIT_CONFIG}" "selected hyphenated component enabled (git-config → GIT_CONFIG)"
assert_eq false "${DEPLOY_TMUX}" "unselected component disabled"
assert_eq false "${DEPLOY_CLAUDE}" "unselected default-on component disabled"

# A component outside the filtered list is left alone, not silently disabled.
assert_eq true "${DEPLOY_VIM}" "component not passed to the menu is untouched"

_init_component_vars
_apply_component_selection install $'core\nai-tools' core zsh ai-tools extras >/dev/null
assert_eq true "${INSTALL_CORE}" "install mode: selected component enabled"
assert_eq true "${INSTALL_AI_TOOLS}" "install mode: hyphenated component enabled"
assert_eq false "${INSTALL_ZSH}" "install mode: unselected component disabled"

# ─── The shell↔binary contract ────────────────────────────────────────────────

# -m1: a second match (e.g. a comment) would make $invocation multi-line, and the
# pipe check below could then fire across two unrelated lines.
invocation="$(grep -m1 -n 'claude-tools select' "$REPO_ROOT/scripts/shared/helpers.sh" || true)"
if [[ -z "$invocation" ]]; then
    fail "helpers.sh no longer invokes 'claude-tools select' — this test is vacuous"
else
    pass "found the claude-tools select invocation"

    # --items keeps fd 0 on the terminal. Piping items in makes stdin a pipe,
    # which pushes crossterm onto a fragile /dev/tty fallback for key input
    # ("Failed to initialize input reader") and the menu silently disappears.
    if [[ "$invocation" == *"--items"* ]]; then
        pass "items are passed via --items, not stdin"
    else
        fail "expected --items in: $invocation"
    fi

    if [[ "$invocation" == *"|"*"claude-tools select"* ]]; then
        fail "items are piped into claude-tools select — stdin must stay on the tty"
    else
        pass "nothing is piped into claude-tools select"
    fi
fi

# The Rust side must accept every flag emitted here; that direction is pinned by
# helpers_sh_passes_only_flags_this_parser_accepts in src/select/mod.rs.
if grep -q 'helpers_sh_passes_only_flags_this_parser_accepts' \
    "$REPO_ROOT/tools/claude-tools/src/select/mod.rs"; then
    pass "the reverse (parser accepts these flags) is pinned in select/mod.rs"
else
    fail "the caller→parser parity test is missing from tools/claude-tools/src/select/mod.rs"
fi

print ""
if ((FAILURES > 0)); then
    print -u2 "$FAILURES check(s) failed"
    exit 1
fi
print "All component menu checks passed"
