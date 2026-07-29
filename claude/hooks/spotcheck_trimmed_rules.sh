#!/usr/bin/env bash
# AC7 behaviour spot-checks: for each rule sentence deleted from the memory
# tier, prove the replacement hook still produces the behaviour.
#
# SCOPE: script-level. Each hook is fed the JSON the harness would send and
# its exit code / stdout is inspected. This is NOT live session enforcement --
# the hooks are not yet wired into the main checkout's settings.json.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 1
H=claude/hooks

bash_json() { python3 -c "
import json, sys
print(json.dumps({'tool_name': 'Bash', 'tool_input': {'command': sys.argv[1]}}))
" "$1"; }

write_json() { python3 -c "
import json, sys
print(json.dumps({'tool_name': 'Write',
                  'tool_input': {'file_path': sys.argv[1], 'content': sys.argv[2]}}))
" "$1" "$2"; }

check() { # desc expected_rc actual_rc
    if [ "$2" = "$3" ]; then printf '  PASS  %s\n' "$1"
    else printf '  FAIL  %s (want rc=%s, got rc=%s)\n' "$1" "$2" "$3"; fi
}

echo "== deleted from safety-and-git.md: 'never git reset --hard / checkout -- / clean -fd' =="
for cmd in 'git reset --hard' 'git checkout -- src/a.py' 'git clean -fd'; do
    rc=0; bash_json "$cmd" | bash "$H/block_destructive_git.sh" >/dev/null 2>&1 || rc=$?
    check "blocks: $cmd" 2 "$rc"
done

echo "== deleted from safety-and-git.md: 'never bare git stash / stash pop' =="
for cmd in 'git stash' 'git stash pop'; do
    rc=0; bash_json "$cmd" | bash "$H/block_destructive_git.sh" >/dev/null 2>&1 || rc=$?
    check "blocks: $cmd" 2 "$rc"
done

echo "== kept in prose, must NOT be blocked (the safe stash workflow) =="
for cmd in 'git stash push -u -m "wip"' 'git stash apply abc123' 'git stash show -p stash@{0}'; do
    rc=0; bash_json "$cmd" | bash "$H/block_destructive_git.sh" >/dev/null 2>&1 || rc=$?
    check "allows: $cmd" 0 "$rc"
done

echo "== deleted from coding-conventions.md: the sys.path.insert safe pattern =="
out=$(write_json /repo/tool.py 'import sys
sys.path.insert(0, "..")
' | bash "$H/nudge_syspath.sh" 2>/dev/null)
if printf '%s' "$out" | grep -q '_bootstrap_path' && printf '%s' "$out" | grep -q '__main__'; then
    printf '  PASS  nudge emits the full safe pattern (_bootstrap_path + __main__ guard)\n'
else
    printf '  FAIL  nudge did not emit the safe pattern\n'
fi
out=$(write_json /repo/ok.py 'def _bootstrap_path():
    import sys
    sys.path.insert(0, "..")

if __name__ == "__main__":
    _bootstrap_path()
' | bash "$H/nudge_syspath.sh" 2>/dev/null)
[ -z "$out" ] && printf '  PASS  silent on the already-correct form\n' \
              || printf '  FAIL  fired on the correct form\n'
