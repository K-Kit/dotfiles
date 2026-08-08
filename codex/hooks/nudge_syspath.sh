#!/usr/bin/env bash
# PostToolUse(Write|Edit) hook: a bare `sys.path.insert` at import time
# crashes the Claude Code session. The safe form defers it into a helper
# that only runs under `if __name__ == "__main__":`.
#
# NUDGE only — never blocks, never exits non-zero. The full safe pattern
# lives here rather than in prose, so coding-conventions.md carries only the
# one-line prohibition.
#
# Fires when written .py content calls sys.path.insert at module scope, or
# calls it indented in a file with no __main__ guard at all. Stays silent
# when the call is indented inside a helper AND a __main__ guard is present.

# shellcheck disable=SC2016  # backticks in the nudge text are literal markdown
set -uo pipefail

INPUT=$(cat)

command -v jq >/dev/null 2>&1 || exit 0

FILE=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // ""' 2>/dev/null) || exit 0
case "$FILE" in
    *.py) ;;
    *) exit 0 ;;
esac
case "$FILE" in
    */node_modules/*|*/.venv/*|*/site-packages/*|*/archive/*|*/vendor/*) exit 0 ;;
esac

# Write → content; Edit → new_string, or every edits[].new_string
CONTENT=$(printf '%s' "$INPUT" | jq -r '
    [ (.tool_input.content // empty),
      (.tool_input.new_string // empty),
      ((.tool_input.edits // []) | .[]? | (.new_string // empty)) ]
    | join("\n")
' 2>/dev/null) || exit 0
[ -n "$CONTENT" ] || exit 0

printf '%s\n' "$CONTENT" | grep -qE 'sys\.path\.insert' || exit 0

FIRE=false
# module-scope call — unambiguously the crashing form
printf '%s\n' "$CONTENT" | grep -qE '^sys\.path\.insert' && FIRE=true
# indented call with no __main__ guard anywhere in the written content
if [ "$FIRE" = false ]; then
    printf '%s\n' "$CONTENT" | grep -qE '^[[:space:]]+sys\.path\.insert' \
        && ! printf '%s\n' "$CONTENT" | grep -qE '^[[:space:]]*if __name__[[:space:]]*==' \
        && FIRE=true
fi

[ "$FIRE" = true ] || exit 0

MSG='`sys.path.insert` at import time crashes the Claude Code session. Wrap it in a helper that only runs under the `__main__` guard:

```python
def _bootstrap_path() -> None:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


if __name__ == "__main__":
    _bootstrap_path()
    main()
```

Better still, make the package importable (`uv run python -m pkg.module`, or a `[project]` entry in `pyproject.toml`) so no path surgery is needed at all.'

jq -n --arg msg "$MSG" '{systemMessage: $msg}'
exit 0
