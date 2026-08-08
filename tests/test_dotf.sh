#!/usr/bin/env bash
# Tests for custom_bins/dotf — the install/deploy front-end.
# Run: bash tests/test_dotf.sh
#
# Hermetic: nothing here runs install.sh or deploy.sh for real. Every
# forwarding test goes through --dry-run, and `dotf update` is pointed at a
# throwaway git fixture via DOTF_DIR.
#
# The assertions that matter most are the argv ones. install.sh/deploy.sh share
# parse_args, whose `--*` catch-all silently turns ANY unrecognised flag into a
# phantom component variable — so a dotf-only flag that leaked through would
# never error, it would just quietly enable a component that doesn't exist.
# Hence full-command equality against a literal, not `[[ $cmd == *--only* ]]`:
# a presence check passes even when the operand is wrong or extra flags rode
# along.

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DOTF="$REPO/custom_bins/dotf"
PASS=0
FAIL=0

# $TMPDIR rather than the repo's tmp/: the update tests `git init` a throwaway
# repo, and writing a nested .git/config under the checkout is denied inside the
# agent sandbox.
TMP_ROOT="${TMPDIR:-/tmp}"
FIXTURE=$(mktemp -d "${TMP_ROOT%/}/dotf-test.XXXXXX") || {
    echo "could not create fixture dir under $TMP_ROOT" >&2; exit 1; }
[[ -n "$FIXTURE" && -d "$FIXTURE" ]] || exit 1
trap 'rm -rf "$FIXTURE"' EXIT

check() {
    local desc="$1" got="$2" want="$3"
    if [[ "$got" == *"$want"* ]]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        printf 'FAIL: %s\n  wanted to contain: %s\n  got: %s\n' \
            "$desc" "$want" "$(printf '%s' "$got" | head -8 | tr '\n' '|')"
    fi
}

check_eq() {
    local desc="$1" got="$2" want="$3"
    if [[ "$got" == "$want" ]]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        printf 'FAIL: %s\n  wanted exactly: [%s]\n  got:            [%s]\n' \
            "$desc" "$want" "$got"
    fi
}

check_missing() {
    local desc="$1" got="$2" unwanted="$3"
    if [[ "$got" != *"$unwanted"* ]]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        printf 'FAIL: %s\n  must NOT contain: %s\n  got: %s\n' "$desc" "$unwanted" "$got"
    fi
}

dotf() { "$DOTF" "$@" 2>&1; }

echo "=== the executable is executable and shellcheck-clean ==="
if [[ -x "$DOTF" ]]; then PASS=$((PASS + 1)); else FAIL=$((FAIL + 1)); echo "FAIL: $DOTF is not executable"; fi
if command -v shellcheck >/dev/null 2>&1; then
    if out=$(shellcheck "$DOTF" 2>&1); then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1)); printf 'FAIL: shellcheck\n%s\n' "$out"
    fi
else
    echo "  (shellcheck not installed — skipped)"
fi

echo "=== --help lists every command ==="
help_out=$(dotf --help)
for cmd in setup install deploy status doctor components update help; do
    check "help mentions '$cmd'" "$help_out" "$cmd"
done
check_eq "bare dotf == dotf --help" "$(dotf)" "$help_out"
check_eq "dotf help  == dotf --help" "$(dotf help)" "$help_out"

echo "=== install/deploy --help defer to the real script's help ==="
check "install --help is install.sh's" "$(dotf install --help)" "Install dotfile dependencies"
check "deploy --help is deploy.sh's"   "$(dotf deploy --help)"  "--allow-worktree-deploy"

echo "=== --dry-run prints the exact command, flags in order ==="
check_eq "install --only zsh tmux" \
    "$(dotf install --dry-run --only zsh tmux)" \
    "$REPO/install.sh --only zsh tmux"
check_eq "deploy --only claude" \
    "$(dotf deploy --only claude --dry-run)" \
    "$REPO/deploy.sh --only claude"
check_eq "deploy keeps flag order" \
    "$(dotf deploy -n --minimal --shell)" \
    "$REPO/deploy.sh --minimal --shell"
check_eq "install with no flags" \
    "$(dotf install -n)" \
    "$REPO/install.sh"
check_eq "setup runs install then deploy" \
    "$(dotf setup --minimal --dry-run)" \
    "$REPO/install.sh --minimal
$REPO/deploy.sh --minimal"

echo "=== dotf-only flags are consumed, never forwarded ==="
# parse_args' --* catch-all would absorb these as phantom components in silence.
out=$(dotf install --dry-run --minimal)
check_missing "--dry-run not forwarded" "$out" "--dry-run"
out=$(dotf deploy -n --minimal)
check_missing "-n not forwarded" "$out" " -n"
check_eq "-n leaves only the real flag" "$out" "$REPO/deploy.sh --minimal"

echo "=== setup: a failing install aborts before deploy ==="
# Otherwise a half-installed machine gets configs deployed onto it.
SETUPFAIL="$FIXTURE/setupfail"
mkdir -p "$SETUPFAIL"
printf '#!/usr/bin/env zsh\necho INSTALL_RAN\nexit 3\n' > "$SETUPFAIL/install.sh"
printf '#!/usr/bin/env zsh\necho DEPLOY_RAN\n'          > "$SETUPFAIL/deploy.sh"
printf '#!/usr/bin/env zsh\n'                           > "$SETUPFAIL/config.sh"
chmod +x "$SETUPFAIL/install.sh" "$SETUPFAIL/deploy.sh"
out=$(DOTF_DIR="$SETUPFAIL" "$DOTF" setup 2>&1); rc=$?
check_eq "install ran, deploy did not" "$out" "INSTALL_RAN"
check_eq "install's exit status propagates" "$rc" "3"

echo "=== setup forwards every flag to BOTH scripts (pinned, not accidental) ==="
# parse_args absorbs a flag the other script doesn't know without erroring, so
# this is a documented quirk (see `dotf setup --help`) rather than a guarantee
# that each flag reaches the script that understands it.
check_eq "install-only flag still reaches deploy.sh" \
    "$(dotf setup -n --force-reinstall)" \
    "$REPO/install.sh --force-reinstall
$REPO/deploy.sh --force-reinstall"
check_eq "deploy-only flag still reaches install.sh" \
    "$(dotf setup -n --append)" \
    "$REPO/install.sh --append
$REPO/deploy.sh --append"
check "setup --help warns about the split" "$(dotf setup --help)" "absorbed silently"

echo "=== components matches the registries in config.sh ==="
# Drift guard: the names dotf prints must equal the names config.sh declares.
registry_names() {
    awk -v want="$1" '
        /^INSTALL_REGISTRY=\(/ { kind="install"; next }
        /^DEPLOY_REGISTRY=\(/  { kind="deploy";  next }
        kind != "" && /^\)/    { kind=""; next }
        kind != "" && /^[[:space:]]*"/ {
            if (want != "all" && want != kind) next
            line=$0
            sub(/^[[:space:]]*"/, "", line)
            split(line, f, "|")
            print f[1]
        }
    ' "$REPO/config.sh" | sort
}
check_eq "install names match" "$(dotf components install --names | sort)" "$(registry_names install)"
check_eq "deploy names match"  "$(dotf components deploy  --names | sort)" "$(registry_names deploy)"
check_eq "all names match"     "$(dotf components --names | sort)"         "$(registry_names all)"

echo "=== components renders the registry columns, not just names ==="
comp_out=$(dotf components)
check "has a header"                "$comp_out" "KIND     NAME"
check "zsh row: platform + default" "$comp_out" "install  zsh              all      true"
check "serena is off by default"    "$(printf '%s\n' "$comp_out" | grep -E '^deploy +serena ')" "false"
check "apps is macos-only"          "$(printf '%s\n' "$comp_out" | grep -E '^install +apps ')" "macos"
check "install filter excludes deploy rows" "$(dotf components install)" "install "
if [[ "$(dotf components install)" != *"deploy  "* ]]; then
    PASS=$((PASS + 1))
else
    FAIL=$((FAIL + 1)); echo "FAIL: components install leaked deploy rows"
fi

echo "=== status reports the checkout it resolved ==="
status_out=$(dotf status)
check "status names the repo"   "$status_out" "$REPO"
check "status shows the branch" "$status_out" "branch"
check "status shows a profile"  "$status_out" "profile"

echo "=== unknown command and unknown subcommand argument are refused ==="
out=$(dotf bogus; echo "rc=$?")
check "unknown command message" "$out" "unknown command 'bogus'"
check "unknown command exits 1" "$out" "rc=1"
out=$(dotf components nope; echo "rc=$?")
check "unknown components arg" "$out" "unknown argument 'nope'"
check "components arg exits 1" "$out" "rc=1"

echo "=== a DOTF_DIR without the scripts is refused, not silently used ==="
mkdir -p "$FIXTURE/notarepo"
out=$(DOTF_DIR="$FIXTURE/notarepo" "$DOTF" status 2>&1; echo "rc=$?")
check "refuses a non-checkout" "$out" "no dotfiles checkout"
check "exits 1"               "$out" "rc=1"

echo "=== update refuses a dirty tree, and --force overrides ==="
REPO_FIXTURE="$FIXTURE/repo"
mkdir -p "$REPO_FIXTURE"
for f in install.sh deploy.sh config.sh; do printf '#!/usr/bin/env zsh\n' > "$REPO_FIXTURE/$f"; done
chmod +x "$REPO_FIXTURE/install.sh" "$REPO_FIXTURE/deploy.sh"
git -C "$REPO_FIXTURE" init -q >/dev/null 2>&1
git -C "$REPO_FIXTURE" add install.sh deploy.sh config.sh >/dev/null 2>&1
git -C "$REPO_FIXTURE" -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
printf 'dirty\n' >> "$REPO_FIXTURE/config.sh"

out=$(DOTF_DIR="$REPO_FIXTURE" "$DOTF" update --dry-run 2>&1; echo "rc=$?")
check "refuses a dirty tree" "$out" "uncommitted changes"
check "dirty refusal exits 1" "$out" "rc=1"
check_missing "did not pull" "$out" "--ff-only"

out=$(DOTF_DIR="$REPO_FIXTURE" "$DOTF" update --force --dry-run 2>&1)
check_eq "--force pulls fast-forward only" "$out" "git -C $REPO_FIXTURE pull --ff-only"

out=$(DOTF_DIR="$REPO_FIXTURE" "$DOTF" update --force --deploy --dry-run 2>&1)
check_eq "--deploy re-deploys after the pull" "$out" "git -C $REPO_FIXTURE pull --ff-only
$REPO_FIXTURE/deploy.sh"

echo "=== a DOTF_DIR that is not itself a checkout is not treated as one ==="
# git -C walks up to an enclosing repo; dotf must not inherit its state.
NOTREPO="$FIXTURE/scripts-only"
mkdir -p "$NOTREPO"
for f in install.sh deploy.sh config.sh; do printf '#!/usr/bin/env zsh\n' > "$NOTREPO/$f"; done
check "status says not a checkout" "$(DOTF_DIR="$NOTREPO" "$DOTF" status 2>&1)" "(not a git checkout)"
out=$(DOTF_DIR="$NOTREPO" "$DOTF" update --dry-run 2>&1; echo "rc=$?")
check "update refuses a non-checkout" "$out" "not the top of a git checkout"
check "non-checkout exits 1"          "$out" "rc=1"
check_missing "did not pull anything" "$out" "--ff-only"

echo "=== update refuses a git worktree even when clean ==="
printf '#!/usr/bin/env zsh\n' > "$REPO_FIXTURE/config.sh"   # undo the dirtying edit
git -C "$REPO_FIXTURE" worktree add -q -b wt "$FIXTURE/wt" >/dev/null 2>&1
if [[ -d "$FIXTURE/wt" ]]; then
    out=$(DOTF_DIR="$FIXTURE/wt" "$DOTF" update --dry-run 2>&1; echo "rc=$?")
    check "refuses a worktree"   "$out" "is a git worktree"
    check "names the main tree"  "$out" "$REPO_FIXTURE"
    check "worktree exits 1"     "$out" "rc=1"
    status_wt=$(DOTF_DIR="$FIXTURE/wt" "$DOTF" status 2>&1)
    check "status warns about the worktree" "$status_wt" "this is a git worktree"
else
    echo "  (git worktree add failed — skipped)"
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
