#!/usr/bin/env bash
# Tests for mask_env_read.sh
# Run: bash claude/hooks/test_mask_env_read.sh
#
# The hook always exits 0 and signals its verdict as JSON on stdout, so these
# tests assert on the presence of a "deny" decision, not on the exit code.

HOOK="$(cd "$(dirname "$0")" && pwd)/mask_env_read.sh"
PASS=0
FAIL=0

# Fixture dir with a real .env, because the hook only masks files that exist.
FIXTURE=$(mktemp -d "${TMPDIR:-/tmp}/mask_env_test.XXXXXX")
trap 'rm -rf "$FIXTURE"' EXIT
printf 'API_KEY=supersecretvalue\nPLAIN=hello\n' > "$FIXTURE/.env"
printf 'export TOKEN=anothersecret\n' > "$FIXTURE/.envrc"
printf 'not an env file\n' > "$FIXTURE/README.md"

run_bash() {
    local desc="$1" cmd="$2" expect="$3"
    local out
    out=$(python3 -c "
import json, sys
print(json.dumps({'tool_name': 'Bash', 'cwd': sys.argv[2],
                  'tool_input': {'command': sys.argv[1]}}))" \
        "$cmd" "$FIXTURE" | bash "$HOOK" 2>/dev/null)
    local got="allow"
    [[ "$out" == *'"deny"'* ]] && got="mask"
    if [ "$got" = "$expect" ]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        printf 'FAIL: %s (expected %s, got %s)\n' "$desc" "$expect" "$got"
    fi
}

run_read() {
    local desc="$1" path="$2" expect="$3"
    local out
    out=$(python3 -c "
import json, sys
print(json.dumps({'tool_name': 'Read', 'tool_input': {'file_path': sys.argv[1]}}))" \
        "$path" | bash "$HOOK" 2>/dev/null)
    local got="allow"
    [[ "$out" == *'"deny"'* ]] && got="mask"
    if [ "$got" = "$expect" ]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        printf 'FAIL: %s (expected %s, got %s)\n' "$desc" "$expect" "$got"
    fi
}

echo "=== SHOULD MASK: Read tool ==="
run_read "read .env"               "$FIXTURE/.env"    mask
run_read "read .envrc"             "$FIXTURE/.envrc"  mask

echo "=== SHOULD MASK: simple Bash reads ==="
run_bash "cat .env"                'cat .env'                 mask
run_bash "head .envrc"             'head .envrc'              mask
run_bash "grep in .env"            'grep API_KEY .env'        mask
run_bash "path-qualified cat"      '/bin/cat .env'            mask

echo "=== SHOULD MASK: reads hidden behind shell operators (the fixed bypass) ==="
run_bash "piped to head"           'cat .env | head -1'       mask
run_bash "piped to wc"             'cat .env | wc -l'         mask
run_bash "grep piped"              'grep KEY .env | cut -d= -f1'  mask
run_bash "behind &&"               'ls -la && cat .env'       mask
run_bash "behind ;"                'pwd; cat .env'            mask
run_bash "behind ||"               'false || cat .envrc'      mask
run_bash "second in a pipeline"    'echo hi | grep -f .env'   mask

echo "=== SHOULD ALLOW: no env file involved ==="
run_bash "cat a normal file"       'cat README.md'            allow
run_bash "normal file piped"       'cat README.md | head -1'  allow
run_bash "git status"              'git status --short'       allow
run_bash "ls with pipe"            'ls -la | wc -l'           allow
run_read "read a normal file"      "$FIXTURE/README.md"       allow
run_bash "nonexistent env file"    'cat .env.missing'         allow

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
