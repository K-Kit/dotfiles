#!/usr/bin/env bash
# Tests for block_unsafe_install.py — the four hard supply-chain gates.
# Each "block" case asserts exit 2 (the call is actually refused), not merely
# that the hook ran.
set -euo pipefail

HOOK="$(cd "$(dirname "$0")" && pwd)/block_unsafe_install.py"
PASS=0
FAIL=0

run_test() {
    local desc="$1" cmd="$2" expect="$3"
    local input
    input=$(python3 -c "
import json, sys
print(json.dumps({'tool_name': 'Bash', 'tool_input': {'command': sys.argv[1]}}))" "$cmd")

    local rc=0
    printf '%s' "$input" | python3 "$HOOK" >/dev/null 2>&1 || rc=$?

    if [ "$expect" = "block" ] && [ "$rc" -eq 2 ]; then
        PASS=$((PASS + 1))
    elif [ "$expect" = "allow" ] && [ "$rc" -eq 0 ]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        printf 'FAIL: %s (expected %s, got exit %d)\n' "$desc" "$expect" "$rc"
    fi
}

echo "=== GATE 1: third-party Homebrew taps ==="
run_test "brew tap" "brew tap someuser/some-repo" "block"
run_test "brew tap with url" "brew tap someuser/repo https://github.com/someuser/repo" "block"
run_test "brew install from tap" "brew install someuser/repo/sometool" "block"
run_test "brew install cask from tap" "brew install --cask someuser/repo/someapp" "block"

echo "=== GATE 2: arbitrary URL / git installs ==="
run_test "pip git+https" "pip install git+https://github.com/foo/bar.git" "block"
run_test "pip https wheel" "pip install https://example.com/pkg-1.0-py3-none-any.whl" "block"
run_test "uv pip git+" "uv pip install git+https://github.com/foo/bar" "block"
run_test "uv add git+" "uv add git+https://github.com/foo/bar" "block"
run_test "python -m pip git+" "python3 -m pip install git+ssh://git@github.com/foo/bar" "block"
run_test "npm github shorthand" "npm install github:foo/bar" "block"
run_test "npm tarball url" "npm install https://example.com/pkg.tgz" "block"
run_test "bun add git+" "bun add git+https://github.com/foo/bar" "block"
run_test "pnpm add url" "pnpm add https://example.com/pkg.tgz" "block"

echo "=== GATE 3: --ignore-scripts=false ==="
run_test "npm ignore-scripts=false" "npm install --ignore-scripts=false lodash" "block"
run_test "npm ignore-scripts false" "npm install --ignore-scripts false lodash" "block"

echo "=== GATE 4: --no-quarantine ==="
run_test "brew cask no-quarantine" "brew install --cask --no-quarantine someapp" "block"

echo "=== Evasion: chained / wrapped commands ==="
run_test "chained with &&" "cd /tmp && brew tap someuser/some-repo" "block"
run_test "chained with ;" "echo hi; pip install git+https://github.com/foo/bar" "block"
run_test "sudo prefix" "sudo pip install git+https://github.com/foo/bar" "block"
run_test "env var prefix" "FOO=bar npm install github:foo/bar" "block"
# Gate 2 reads the whole token list, so the two cases above passed even while
# Gate 1 — which keys on the command NAME — permitted every wrapped form.
# Wrapping is the cheapest evasion there is, so each name-keyed gate needs its
# own prefixed case; a shared "evasion" section that only exercises one gate
# reads as coverage and is not.
run_test "gate1 sudo prefix" "sudo brew tap someuser/some-repo" "block"
run_test "gate1 sudo + install" "sudo brew install someuser/repo/sometool" "block"
run_test "gate1 env var prefix" "HOMEBREW_NO_AUTO_UPDATE=1 brew install someuser/repo/sometool" "block"
run_test "gate1 sudo with flag" "sudo -H brew tap someuser/some-repo" "block"
run_test "gate1 sudo -u with value" "sudo -u admin brew tap someuser/some-repo" "block"
run_test "gate1 env command wrapper" "env HOMEBREW_NO_AUTO_UPDATE=1 brew tap someuser/some-repo" "block"
run_test "gate1 stacked wrappers" "sudo env FOO=1 brew tap someuser/some-repo" "block"
run_test "gate1 absolute path binary" "/opt/homebrew/bin/brew tap someuser/some-repo" "block"
run_test "gate1 wrapped, chained" "cd /tmp && sudo brew install someuser/repo/tool" "block"
run_test "gate4 sudo prefix" "sudo brew install --cask --no-quarantine someapp" "block"

echo "=== Evasion: wrappers must not create FALSE blocks ==="
run_test "sudo plain brew install" "sudo brew install ripgrep" "allow"
run_test "env prefix plain pip" "FOO=bar pip install requests" "allow"
run_test "sudo pip index-url" "sudo pip install -i https://pypi.org/simple requests" "allow"
run_test "env wrapper unrelated" "env FOO=1 git status" "allow"

echo "=== SHOULD ALLOW: ordinary installs ==="
run_test "plain pip install" "pip install requests" "allow"
run_test "pip requirements file" "pip install -r requirements.txt" "allow"
run_test "pip editable local" "pip install -e ." "allow"
run_test "pip with index-url" "pip install -i https://pypi.org/simple requests" "allow"
run_test "uv add plain" "uv add pydantic" "allow"
run_test "uv sync" "uv sync" "allow"
run_test "npm install plain" "npm install lodash" "allow"
run_test "npm install no args" "npm install" "allow"
run_test "bun add plain" "bun add zod" "allow"
run_test "brew install core formula" "brew install ripgrep" "allow"
run_test "brew tap no args (lists)" "brew tap" "allow"
run_test "brew untap" "brew untap someuser/repo" "allow"
run_test "brew install cask official" "brew install --cask ghostty" "allow"
run_test "npm registry flag" "npm install --registry https://registry.npmjs.org lodash" "allow"
run_test "unrelated command" "git status" "allow"
run_test "help flag" "brew tap --help" "allow"
run_test "dry run" "brew install someuser/repo/tool --dry-run" "allow"

echo
printf 'PASS: %d  FAIL: %d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
