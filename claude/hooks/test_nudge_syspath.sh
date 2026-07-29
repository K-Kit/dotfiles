#!/usr/bin/env bash
# Tests for nudge_syspath.sh.
#
# Same contract as the other convention nudges: fire a systemMessage on a
# positive, stay silent on negatives, NEVER exit non-zero. The fired message
# must carry the full safe pattern — the prose rule only says "don't", so the
# hook is where the "do this instead" lives.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK="$DIR/nudge_syspath.sh"
PASS=0
FAIL=0

# tool_json <tool> <path> <key> <value> — values via argv, never interpolated.
tool_json() {
    python3 -c "
import json, sys
tool, path, key, value = sys.argv[1:5]
print(json.dumps({'tool_name': tool, 'tool_input': {'file_path': path, key: value}}))
" "$1" "$2" "$3" "$4"
}
post_write() { tool_json Write "$1" content "$2"; }
post_edit()  { tool_json Edit  "$1" new_string "$2"; }

run() {
    local desc="$1" input="$2" expect="$3"
    local out rc=0
    out=$(printf '%s' "$input" | bash "$HOOK" 2>/dev/null) || rc=$?

    if [ "$rc" -ne 0 ]; then
        FAIL=$((FAIL + 1))
        printf 'FAIL: %s (nudge hook exited %d — must always be 0)\n' "$desc" "$rc"
        return
    fi

    local fired=silent
    case "$out" in *systemMessage*) fired=fire ;; esac

    if [ "$fired" != "$expect" ]; then
        FAIL=$((FAIL + 1))
        printf 'FAIL: %s (expected %s, got %s)\n' "$desc" "$expect" "$fired"
        return
    fi

    if [ "$expect" = fire ]; then
        # AC3: the nudge must quote the safe pattern, not merely forbid
        if ! printf '%s' "$out" | grep -q '__main__'; then
            FAIL=$((FAIL + 1))
            printf 'FAIL: %s (nudge does not quote the __main__ guard pattern)\n' "$desc"
            return
        fi
        if ! printf '%s' "$out" | grep -q '_bootstrap_path'; then
            FAIL=$((FAIL + 1))
            printf 'FAIL: %s (nudge does not quote the helper pattern)\n' "$desc"
            return
        fi
    fi

    PASS=$((PASS + 1))
}

echo "=== violating writes (must fire) ==="

MODULE_SCOPE='import sys
sys.path.insert(0, "..")

def main():
    pass'
run "module-scope insert" "$(post_write /repo/run.py "$MODULE_SCOPE")" fire

NO_GUARD='def setup():
    import sys
    sys.path.insert(0, "..")

setup()'
run "indented insert, no __main__ guard" "$(post_write /repo/run.py "$NO_GUARD")" fire

run "Edit new_string violates" "$(post_edit /repo/run.py "$MODULE_SCOPE")" fire

echo "=== safe / irrelevant writes (must stay silent) ==="

GUARDED='def _bootstrap_path() -> None:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


if __name__ == "__main__":
    _bootstrap_path()
    main()'
run "guarded helper pattern" "$(post_write /repo/run.py "$GUARDED")" silent

run "no sys.path at all" \
    "$(post_write /repo/run.py 'import os

def main():
    return os.getcwd()')" silent

run "sys.path.append is not insert" \
    "$(post_write /repo/run.py 'import sys
sys.path.append("..")')" silent

run "non-python file" "$(post_write /repo/notes.md "$MODULE_SCOPE")" silent
run "vendored path skipped" "$(post_write /repo/node_modules/x/run.py "$MODULE_SCOPE")" silent
run "archived path skipped"  "$(post_write /repo/archive/old.py "$MODULE_SCOPE")" silent
run "no file_path"  '{"tool_name":"Write","tool_input":{}}' silent
run "non-dict JSON" '[]' silent
run "empty content" "$(post_write /repo/run.py '')" silent

echo
TOTAL=$((PASS + FAIL))
echo "Results: $PASS passed, $FAIL failed (total $TOTAL)"
[ "$FAIL" -eq 0 ] && echo "All tests passed!"
[ "$FAIL" -eq 0 ]
