#!/usr/bin/env bash
# F1 — substitution-class guard.
# PreToolUse(Write): when about to create a NEW source file, nudge to check
# whether existing code already does this.
#
# The failure mode: asked to run an experiment, write a fresh ad-hoc script
# instead of using the validated existing pipeline — then report results from
# the ad-hoc one. That substitutes "code I just wrote" for "code that was
# validated", silently. See CLAUDE.md § Default Behaviors (use existing code).
#
# NUDGE only — exit 0 always. Creating new files is normal; the point is to
# make the check conscious, not to gate it.

set -euo pipefail

SOURCE_EXT_RE='\.(py|sh|ts|tsx|js|mjs|rs|R)$'

# Areas where a new file is expected and carries no substitution risk.
SKIP_PATH_RE='(^|/)(tests?|__tests__|node_modules|\.venv|__pycache__|tmp|archive|migrations|\.git)(/|$)|(^|/)(test_|conftest\.py$)'

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

# Only nudge on NEW files — editing an existing one is not a substitution.
[ -e "$FILE_PATH" ] && exit 0

[[ "$FILE_PATH" =~ $SOURCE_EXT_RE ]] || exit 0
[[ "$FILE_PATH" =~ $SKIP_PATH_RE ]] && exit 0

BASE=$(basename "$FILE_PATH")

cat <<HOOK_EOF
{
  "systemMessage": "NUDGE: creating a new source file ($BASE). Before writing it, confirm no existing code already does this — Grep for the core verb/noun in the repo. For experiments, use the existing validated pipeline with correct hyperparams and full data rather than a fresh ad-hoc script; ad-hoc is for dry runs only."
}
HOOK_EOF
exit 0
