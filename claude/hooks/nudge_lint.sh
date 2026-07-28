#!/usr/bin/env bash
# Lint nudge — enforces coding-conventions.md's "ruff for Python, shellcheck
# before committing shell" at write time instead of relying on recall.
#
# PostToolUse(Write|Edit): lint the file that was just written. `.py` → ruff,
# `.sh` → shellcheck. Skips silently when the linter isn't installed.
#
# NUDGE only — always exit 0. Lint findings are feedback, not a gate; a broken
# linter or a slow filesystem must never surface as a hook error.

set -uo pipefail

INPUT=$(cat)

FILE_PATH=$(printf '%s' "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print((d.get('tool_input', d) or {}).get('file_path', ''))
except Exception:
    print('')
" 2>/dev/null) || exit 0

[ -z "$FILE_PATH" ] && exit 0
[ -f "$FILE_PATH" ] || exit 0

# Vendored / generated code is not ours to lint.
case "$FILE_PATH" in
    */node_modules/*|*/.venv/*|*/__pycache__/*|*/.git/*) exit 0 ;;
esac

# Bound linter runtime below the external hook timeout. GNU `timeout` is absent
# on stock macOS (coreutils installs it as `gtimeout`), and an unbounded linter
# would be killed by the harness with a visible status-124 error — so the
# watchdog is python3, which this hook already requires.
run_lint() {
    python3 -c '
import subprocess, sys
try:
    r = subprocess.run(sys.argv[1:], stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL, text=True, timeout=4)
    sys.stdout.write(r.stdout or "")
except Exception:
    pass
' "$@"
}

FINDINGS=""
TOOL=""
case "$FILE_PATH" in
    *.py)
        command -v ruff >/dev/null 2>&1 || exit 0
        TOOL="ruff"
        FINDINGS=$(run_lint ruff check --quiet --no-fix "$FILE_PATH") || true
        ;;
    *.sh)
        command -v shellcheck >/dev/null 2>&1 || exit 0
        TOOL="shellcheck"
        FINDINGS=$(run_lint shellcheck -f gcc "$FILE_PATH") || true
        ;;
    *) exit 0 ;;
esac

[ -z "$FINDINGS" ] && exit 0

N=$(printf '%s\n' "$FINDINGS" | grep -c .)
HEAD=$(printf '%s\n' "$FINDINGS" | head -3)

printf '%s' "$HEAD" | python3 -c "
import sys, json
head = sys.stdin.read()
tool, n, path = sys.argv[1], sys.argv[2], sys.argv[3]
base = path.rsplit('/', 1)[-1]
print(json.dumps({'systemMessage':
    f'NUDGE: {tool} reports {n} finding(s) in {base}:\n{head}\n'
    f'Fix before committing (coding-conventions.md).'}))
" "$TOOL" "$N" "$FILE_PATH"
exit 0
