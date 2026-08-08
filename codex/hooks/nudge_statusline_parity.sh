#!/usr/bin/env bash
# PostToolUse nudge: the statusline has two implementations that must agree.
#
# tools/claude-tools/src/statusline.rs is what actually runs; claude/statusline.sh
# is the fallback for when the binary is missing or fails. Both files carry a
# comment pointing at the other, but a comment is only read by whoever opens the
# file — editing one and shipping without the other produces no error anywhere,
# just a fallback that silently renders a different statusline.
#
# Reads the PostToolUse payload on stdin; stays silent unless the edited path is
# one of the pair. Always exits 0 — this is advice, not a gate.

set -euo pipefail

payload=$(cat 2>/dev/null || true)
[[ -n "$payload" ]] || exit 0

path=$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
print(data.get("tool_input", {}).get("file_path", "") or "")
' 2>/dev/null || true)

[[ -n "$path" ]] || exit 0

case "$path" in
    */tools/claude-tools/src/statusline.rs)
        cat <<'EOF'
⚠ Statusline parity: you edited the Rust statusline (the one that actually runs).
  Mirror the change in claude/statusline.sh (the fallback), then rebuild and
  commit the binary — scripts/check-claude-tools-fresh.sh reports which
  platforms are stale.
EOF
        ;;
    */claude/statusline.sh)
        cat <<'EOF'
⚠ Statusline parity: you edited the bash fallback. The Rust implementation at
  tools/claude-tools/src/statusline.rs is what renders in practice — mirror the
  change there, or the edit will not be visible.
EOF
        ;;
esac

exit 0
