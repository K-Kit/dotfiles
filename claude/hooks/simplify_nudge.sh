#!/usr/bin/env bash
# Stop hook: if a code file changed this turn (marked by
# simplify_mark_dirty.sh), surface a systemMessage suggesting a simplify pass.
# Soft nudge only — never blocks the stop.
#
# systemMessage, not additionalContext, and deliberately so. additionalContext
# forces the turn to continue, which made every code-writing turn pay for an
# extra pass whether or not the code needed one — the "harness scaffolding that
# adds a separate pass" pattern current models are explicitly worse off for.
# A systemMessage puts the suggestion in front of the user and stops there.
#
# Because nothing continues the turn, the stop_hook_active guard this hook used
# to carry is moot: a simplify pass can no longer re-mark the session dirty and
# re-trigger itself on the next Stop check. Don't reinstate it with the message
# type as-is.
set -euo pipefail

INPUT=$(cat)

SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null) || exit 0
[ -z "$SESSION_ID" ] && exit 0

MARKER="${TMPDIR:-/tmp}/claude-simplify-dirty-${SESSION_ID}"
[ -f "$MARKER" ] || exit 0
rm -f "$MARKER" 2>/dev/null || true

cat <<'HOOK_EOF'
{
  "systemMessage": "Code changed this turn — `/simplify` will run a quality pass over the changed files if it's worth one."
}
HOOK_EOF
exit 0
