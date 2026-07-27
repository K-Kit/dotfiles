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

# --- Regression: the four fail-open paths found by review on 2026-07-27 -------
# Every case below was PERMITTED while this suite was green. That is the point:
# a passing suite was not evidence the gates worked, so each probe lives here
# permanently rather than in a scratch script outside the repo.
echo "=== Regression: flag arity must not be guessed ==="
# `sudo -n` is boolean; `nice -n 10` takes a value. The two are token-identical,
# so ANY single skip table that decides how many tokens to consume after `-n`
# silently permits one of them. The fix stopped keying on tokens[0] and now
# tests every candidate command start — which is exactly what this pair checks.
# Keep BOTH: dropping either one makes a wrong skip table look correct again.
run_test "sudo -n (boolean flag)" "sudo -n pip install git+https://github.com/foo/bar" "block"
run_test "nice -n 10 (flag takes a value)" "nice -n 10 brew tap someuser/some-repo" "block"
run_test "nice -n 10 must not false-block" "nice -n 10 brew install ripgrep" "allow"

echo "=== Regression: remote requirements files ==="
# -r takes a URL as readily as a path, and the URL is the payload: pip fetches
# and installs whatever that server returns. Gate 2 scanned install targets but
# not the argument to -r.
run_test "pip -r remote url" "pip install -r https://example.com/reqs.txt" "block"
run_test "uv pip -r remote url" "uv pip install -r https://example.com/reqs.txt" "block"
run_test "pip -r local file still allowed" "pip install -r requirements.txt" "allow"

echo "=== Regression: nested shell -c ==="
# `bash -c '<cmd>'` keeps the whole nested command as ONE shlex token, invisible
# to every token-level gate unless the hook re-enters it. Chaining operators
# INSIDE the nested string are likewise invisible to the outer split.
run_test "bash -c nested url install" \
    "bash -c 'pip install git+https://github.com/foo/bar'" "block"
run_test "sh -c nested tap" "sh -c 'brew tap someuser/some-repo'" "block"
run_test "bash -c nested, chained inside" \
    "bash -c 'cd /tmp && npm install github:foo/bar'" "block"
run_test "bash -c benign must not block" "bash -c 'pip install requests'" "allow"

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

# --- Regression: fail-open paths found by review 2 (2026-07-27) ---------------
# Every case below was PERMITTED while the 61 assertions above were green. That
# is the second time a full-green suite coexisted with live bypasses, so the
# lesson is recorded here rather than in a commit message: coverage of the
# defects a review NAMED is not coverage of the defect CLASS. Each block states
# the class, not just the instance.

echo "=== Regression: safe flags must not out-scope a nested command ==="
# Bash gives `--help` to the WRAPPER as $0 and still executes the -c string. An
# outer safe-flag check that runs before nested traversal therefore exempts the
# payload from every gate. Nested strings are now extracted first, and a safe
# flag only ever exempts the segment that literally carries it.
run_test "outer --help must not exempt nested tap" \
    "bash -c 'brew tap evil/repo' --help" "block"
run_test "outer --dry-run must not exempt nested install" \
    "bash -c 'pip install git+https://github.com/foo/bar' --dry-run" "block"
run_test "--help inside the nested command still exempts" \
    "bash -c 'brew tap --help'" "allow"

echo "=== Regression: shell option clusters run -c too ==="
# `bash -lc CMD` and `sh -ec CMD` execute CMD exactly as `bash -c CMD` does.
# Matching only the exact token `-c` let every clustered spelling through.
run_test "bash -lc nested" "bash -lc 'npm install github:foo/bar'" "block"
run_test "sh -ec nested" "sh -ec 'brew tap evil/repo'" "block"
run_test "zsh -ic nested" "zsh -ic 'pip install git+https://github.com/foo/bar'" "block"
run_test "bash running a script is not -c" "bash deploy.sh" "allow"

echo "=== Regression: installer-global options precede the subcommand ==="
# Reading args[0] assumes the subcommand is first. Every installer accepts
# global options before it, and `uv tool install` puts a noun there, so the
# subcommand is now SEARCHED for rather than positionally assumed.
run_test "npm global --prefix before install" \
    "npm --prefix /tmp install github:foo/bar" "block"
run_test "pip global --isolated before install" \
    "pip --isolated install git+https://github.com/foo/bar" "block"
run_test "uv global --quiet before add" \
    "uv --quiet add git+https://github.com/foo/bar" "block"
run_test "uv tool install" "uv tool install git+https://github.com/foo/bar" "block"
run_test "uv tool install plain still allowed" "uv tool install ruff" "allow"

echo "=== Regression: flag arity is installer-specific, not universal ==="
# npm's -f is --force and its -p is --parseable (both boolean); pip's -f is
# --find-links and its -p is --python (both take a value). One shared table
# skipped the token after npm's -f — exactly where the package sits. The two
# pairs are kept as separate assertions on purpose: collapsing them is what made
# a single wrong table look correct.
run_test "npm -f is boolean, not --find-links" "npm install -f github:foo/bar" "block"
run_test "npm -p is boolean, not a path value" "npm install -p github:foo/bar" "block"
run_test "pip -f IS --find-links (value skipped)" \
    "pip install -f https://example.com/wheels requests" "allow"
run_test "pip -p IS --python (value skipped)" "pip install -p /usr/bin/python3 requests" "allow"

echo "=== Regression: npm git specs carry no URL scheme ==="
# `npm i foo/bar` is documented GitHub shorthand, `git@host:path` is an scp-style
# git URL, and `alias@github:owner/repo` hides the host after the separator. A
# scheme-anchored regex saw none of them.
run_test "npm owner/repo shorthand" "npm install foo/bar" "block"
run_test "npm scp-style git url" "npm install git@github.com:foo/bar.git" "block"
run_test "npm alias@github: spec" "npm install foo@github:bar/baz" "block"
run_test "npm scoped package is not a git spec" "npm install @types/node" "allow"
run_test "npm local path is not a git spec" "npm install ./packages/core" "allow"
run_test "pip owner/repo is a local path, not a git spec" "pip install foo/bar" "allow"

echo "=== Regression: operators inside quotes are not separators ==="
# Splitting the RAW string on `;` tore a quoted URL into unbalanced fragments,
# after which the URL regex could not match. Lexing now happens first.
run_test "quoted ; inside a wheel url" \
    "pip install 'https://example.com/pkg.whl;param'" "block"
run_test "quoted && inside a package name" \
    "pip install 'https://example.com/a&&b.whl'" "block"
run_test "real ; still splits segments" "echo hi ; brew tap evil/repo" "block"

echo "=== Regression: ignore-scripts has more than two spellings ==="
# `--no-ignore-scripts` (boolean negation) and the npm_config_* environment form
# are the same instruction to npm as `--ignore-scripts=false`.
run_test "boolean-negation form" "npm install --no-ignore-scripts lodash" "block"
run_test "env form, uppercase" "NPM_CONFIG_IGNORE_SCRIPTS=false npm install lodash" "block"
run_test "env form, lowercase" "npm_config_ignore_scripts=0 npm install lodash" "block"
run_test "ignore-scripts=true is the DEFENSE, not a bypass" \
    "npm install --ignore-scripts=true lodash" "allow"

echo "=== Regression: official homebrew taps must NOT be blocked ==="
# Over-blocking is a defect too: policy explicitly ALLOWS official core formulae
# and casks, so blocking them trains the user to work around the hook.
run_test "official core tap" "brew tap homebrew/core" "allow"
run_test "official cask tap" "brew tap homebrew/cask" "allow"
run_test "official tapped formula" "brew install homebrew/core/ripgrep" "allow"
run_test "third-party tap still blocked" "brew tap evil/repo" "block"
run_test "third-party tapped formula still blocked" "brew install evil/repo/tool" "block"

echo "=== Regression: a NEWLINE is a command separator ==="
# Review 3, P1. shlex counts \n as ordinary whitespace, so a multiline command
# lexed into ONE segment and a `--help` on line 1 donated its exemption to an
# install on line 2 — check_segment returned before Gate 2 while bash ran both.
# The class is "separator not recognised", so every separator spelling is tested,
# not just the \n + --help instance that was reported.
run_test "newline + --help shadow" \
    $'echo --help\npip install https://example.com/pkg.whl' "block"
run_test "newline + --dry-run shadow" \
    $'echo --dry-run\nbrew tap evil/repo' "block"
run_test "newline + -h shadow" \
    $'echo -h\nnpm install github:foo/bar' "block"
run_test "bare CR separates" \
    $'echo --help\rpip install git+https://github.com/foo/bar' "block"
run_test "CRLF separates" \
    $'echo --help\r\nbrew tap evil/repo' "block"
run_test "newline with no surrounding spaces" \
    $'echo --help\npip install git+https://github.com/foo/bar' "block"
run_test "three lines, payload last" \
    $'cd /tmp\necho --help\nbrew tap evil/repo' "block"
run_test "newline inside a nested -c payload" \
    "bash -c 'echo --help
pip install git+https://github.com/foo/bar'" "block"
# The exemption must still work WITHIN its own line, or this becomes over-blocking.
run_test "safe flag still exempts its own line" \
    $'pip install --help\necho done' "allow"
run_test "multiline with nothing to block" \
    $'cd /tmp\nnpm install lodash\necho done' "allow"
# The other half of the requirement: a newline that is QUOTED or escaped is data,
# not a separator. Naive string-splitting would have satisfied the tests above
# while breaking these, which is why the fix lives in the lexer.
run_test "single-quoted newline is data" $'echo \'a\nb\' && echo ok' "allow"
run_test "double-quoted newline is data" $'echo "a\nb" && echo ok' "allow"
run_test "quoted newline does not split a URL" \
    $'pip install \'https://example.com/a\nb.whl\'' "block"
# Unbalanced quotes take the fallback path; it must split per line too, or
# appending a stray quote reopens the exact shadowing bypass above.
run_test "fallback path still splits on newline" \
    $'echo --help\npip install git+https://github.com/foo/bar \'' "block"
# Operators with no surrounding whitespace. These already held, and are pinned
# because the fix had to re-declare shlex's default punctuation set by hand —
# dropping ; & | from it would lex `hi;brew` as one token.
run_test "no-space ; still splits" "echo hi;brew tap evil/repo" "block"
run_test "no-space && still splits" \
    "echo hi&&pip install git+https://github.com/foo/bar" "block"

echo "=== Regression: an explicit tap remote voids the homebrew/* exemption ==="
# Review 3, P1. The exemption is about the NAME resolving to the official remote.
# Supply a remote and the name guarantees nothing, so `brew tap homebrew/evil
# <attacker-url>` was allowed. The exemption is now refused whenever a remote is
# present rather than the URL being validated — validation would be a new
# parsing surface, and this is the fail-closed direction.
run_test "official-looking name, attacker remote" \
    "brew tap homebrew/evil https://github.com/attacker/repo" "block"
run_test "official name WITH a remote is still refused" \
    "brew tap homebrew/core https://github.com/Homebrew/homebrew-core" "block"
run_test "remote after a flag" \
    "brew tap --force homebrew/x https://github.com/attacker/repo" "block"
run_test "git remote spelling" \
    "brew tap homebrew/x git@github.com:attacker/repo.git" "block"
# The exemption itself must survive: blocking these is what gets a hook deleted.
run_test "official tap, no remote, still allowed" "brew tap homebrew/core" "allow"
run_test "official cask tap, no remote, still allowed" "brew tap homebrew/cask" "allow"
run_test "official tapped formula still allowed" "brew install homebrew/cask/ghostty" "allow"

echo "=== Regression: workspace/filter flags take a value (no false denials) ==="
# Review 3, P2. `npm install --workspace packages/web lodash` was BLOCKED: the
# flag's VALUE was read as a package spec and matched npm's owner/repo shorthand.
# A false denial is what actually gets a hook deleted, so over-blocking is
# treated as a defect of the same severity as a bypass.
run_test "npm --workspace" "npm install --workspace packages/web lodash" "allow"
run_test "npm -w short form" "npm install -w packages/web lodash" "allow"
run_test "npm --workspace= inline" "npm install --workspace=packages/web lodash" "allow"
run_test "npm --omit" "npm install --omit dev lodash" "allow"
run_test "npm --include" "npm install --include optional lodash" "allow"
run_test "npm --loglevel" "npm install --loglevel silly lodash" "allow"
run_test "pnpm --filter" "pnpm add --filter packages/web lodash" "allow"
run_test "pnpm -F short form" "pnpm add -F packages/web lodash" "allow"
run_test "pnpm --dir" "pnpm add --dir packages/web lodash" "allow"
run_test "bun --filter" "bun add --filter packages/web lodash" "allow"
# Arity in the OTHER direction: skipping one token too many is a fail-open. Each
# of these puts a real payload immediately after the flag's value.
run_test "npm --workspace skips exactly one token" \
    "npm install --workspace packages/web github:foo/bar" "block"
run_test "npm -w skips exactly one token" \
    "npm install -w packages/web git+https://github.com/foo/bar" "block"
run_test "npm --omit skips exactly one token" \
    "npm install --omit dev github:foo/bar" "block"
run_test "pnpm --filter skips exactly one token" \
    "pnpm add --filter packages/web github:foo/bar" "block"
# Flags that are BOOLEAN must not be added to the tables: listing them would skip
# the package spec. npm --workspaces (plural) and --include-workspace-root are
# booleans even though --workspace (singular) is not.
run_test "npm --workspaces is boolean" \
    "npm install --workspaces github:foo/bar" "block"
run_test "npm --include-workspace-root is boolean" \
    "npm install --include-workspace-root github:foo/bar" "block"
# The sharpest case for per-TOOL tables: -w takes a value for npm and is a
# boolean for pnpm (--workspace-root). One shared "node" table cannot be right
# for both — whichever way it is set, one of these two assertions fails.
run_test "pnpm -w is boolean, payload still caught" \
    "pnpm add -w github:foo/bar" "block"
run_test "yarn -W is boolean, payload still caught" \
    "yarn add -W github:foo/bar" "block"
# ...and the allow direction of the same split. Listing pnpm's -w as value-taking
# would eat "lodash" and leave nothing to check, which passes for the wrong
# reason; this fails loudly instead if the pnpm table ever inherits npm's.
run_test "pnpm -w is boolean, safe package still allowed" "pnpm add -w lodash" "allow"
# Splitting the tables by tool means is_remote_pkg must key on the FAMILY. If it
# still compared against the literal "node", every scheme-less npm git spelling
# would silently switch off — these four are the tripwire for that.
run_test "npm scheme-less shorthand still caught" "npm install foo/bar" "block"
run_test "pnpm scheme-less shorthand still caught" "pnpm add foo/bar" "block"
run_test "bun scheme-less shorthand still caught" "bun add foo/bar" "block"
run_test "yarn scheme-less shorthand still caught" "yarn add foo/bar" "block"
# The other side of that heuristic: a relative path also contains a slash. If the
# scheme-less check ever loosens to "any token with a /", local installs break.
run_test "local relative path is not a git spec" "npm install ./local/path" "allow"

echo "=== Regression: gate cost must not be quadratic in token count ==="
# Review 3, P2. candidate_starts() built `tokens[i:]` per token and retained
# every copy, so a BENIGN 8,000-token command cost ~259 MB. A hook that is
# OOM-killed exits non-zero-non-2, which the harness treats as "not blocked" —
# so this is a fail-open with extra steps, not merely a slow path.
#
# The assertion is on MEMORY, deliberately. Wall time was only 0.41s before the
# fix, so any plausible time limit would pass against the old code and witness
# nothing; peak RSS separates them by ~17x.
run_perf_test() {
    local desc="$1" n="$2" max_mb="$3"
    local out
    out=$(python3 -c '
import json, resource, subprocess, sys, time
hook, n = sys.argv[1], int(sys.argv[2])
cmd = "echo " + " ".join("tok%d" % i for i in range(n))
payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
t0 = time.time()
p = subprocess.run([sys.executable, hook], input=payload,
                   capture_output=True, text=True)
dt = time.time() - t0
peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
# ru_maxrss is bytes on macOS, kilobytes on Linux.
mb = peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024
print("%d %.1f %.2f" % (p.returncode, mb, dt))
' "$HOOK" "$n") || { FAIL=$((FAIL + 1)); printf 'FAIL: %s (harness error)\n' "$desc"; return; }

    local rc mb dt
    read -r rc mb dt <<<"$out"
    if [ "$rc" -ne 0 ]; then
        FAIL=$((FAIL + 1))
        printf 'FAIL: %s (benign command not allowed, exit %s)\n' "$desc" "$rc"
    elif [ "${mb%.*}" -ge "$max_mb" ]; then
        FAIL=$((FAIL + 1))
        printf 'FAIL: %s (peak RSS %s MB >= %s MB limit)\n' "$desc" "$mb" "$max_mb"
    elif [ "${dt%.*}" -ge 3 ]; then
        FAIL=$((FAIL + 1))
        printf 'FAIL: %s (wall %ss >= 3s budget)\n' "$desc" "$dt"
    else
        PASS=$((PASS + 1))
    fi
}
# ~259 MB before the fix, ~15 MB after; 80 MB separates them with margin at both
# ends and is well under any realistic hook budget.
run_perf_test "8000-token benign command stays under 80 MB" 8000 80

echo
printf 'PASS: %d  FAIL: %d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
