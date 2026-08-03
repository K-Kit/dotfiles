#!/usr/bin/env bash
# Tests the approval-classifier statusline segment in BOTH implementations.
#
# The Rust binary is what actually renders; claude/statusline.sh is the fallback.
# The point of this file is the final block: it asserts the two produce the same
# segment for the same health file. A test of only one of them would pass while
# the pair silently diverged — which is the exact failure the fallback exists to
# prevent.
#
# Hermetic: uses a fake HOME (both implementations read the health file relative
# to $HOME) and an explicit DOT_DIR, so no real cache or machine conf is touched.
#
# Run: bash tests/test_statusline_classifier.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUST_BIN="$REPO/tools/claude-tools/target/release/claude-tools"
BASH_SL="$REPO/claude/statusline.sh"
PASS=0
FAIL=0

TMP_ROOT="$REPO/tmp"
mkdir -p "$TMP_ROOT"
FAKE=$(mktemp -d "$TMP_ROOT/statusline.XXXXXX") || {
    echo "could not create fixture dir under $TMP_ROOT" >&2; exit 1; }
[[ -n "$FAKE" && -d "$FAKE" ]] || exit 1
trap 'rm -rf "$FAKE"' EXIT

mkdir -p "$FAKE/.cache/claude" "$FAKE/dot/config"
HEALTH="$FAKE/.cache/claude/approval-classifier-health.json"

# A machine conf with a blocked key ahead of the active one, so the label test
# also proves `!`-blocked entries are skipped rather than reported as active.
printf '%s\n' \
    '# machine conf' \
    'ANTHROPIC_API_KEY = !ANTHROPIC_API_KEY - blockedone' \
    'ANTHROPIC_API_KEY = ANTHROPIC_API_KEY - mats' \
    > "$FAKE/dot/config/secrets-global.conf"

STATUS_INPUT='{"model":{"display_name":"Opus"},"workspace":{"current_dir":"'"$FAKE"'"},"cost":{"total_duration_ms":0},"context_window":{"used_percentage":0}}'

write_health() {  # backend, age_seconds
    local backend="$1" age="${2:-0}"
    python3 - "$HEALTH" "$backend" "$age" <<'PY'
import json, sys, time
path, backend, age = sys.argv[1], sys.argv[2], int(sys.argv[3])
with open(path, "w") as f:
    json.dump({"backend": backend, "detail": "", "ts": int(time.time()) - age}, f)
PY
}

# Strip ANSI so assertions read cleanly; keep the raw form for the parity check.
strip_ansi() { sed -e 's/\x1b\[[0-9;]*m//g'; }

render_rust() {
    printf '%s' "$STATUS_INPUT" | \
        env HOME="$FAKE" DOT_DIR="$FAKE/dot" "$RUST_BIN" statusline 2>/dev/null
}

render_bash() {
    printf '%s' "$STATUS_INPUT" | \
        env HOME="$FAKE" DOT_DIR="$FAKE/dot" bash "$BASH_SL" 2>/dev/null
}

# The classifier segment is the only part of the line that mentions "auto".
classifier_segment() {
    strip_ansi | tr '·' '\n' | grep -o 'auto[^ ]*\( ([^)]*)\)\?' | head -1 | sed 's/[[:space:]]*$//'
}

check() {
    local desc="$1" got="$2" want="$3"
    if [[ "$got" == "$want" ]]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        printf 'FAIL: %s\n  wanted: [%s]\n  got:    [%s]\n' "$desc" "$want" "$got"
    fi
}

[[ -x "$RUST_BIN" ]] || {
    echo "SKIP: $RUST_BIN not built — run: cargo build --release --manifest-path tools/claude-tools/Cargo.toml" >&2
    exit 0
}

echo "=== healthy API renders a minimal segment naming the active key ==="
write_health api
check "rust: api shows the key label" "$(render_rust | classifier_segment)" "auto:mats"
check "bash: api shows the key label" "$(render_bash | classifier_segment)" "auto:mats"

echo "=== degraded to subscription is visible and names the failed key ==="
write_health subscription
check "rust: subscription" "$(render_rust | classifier_segment)" "auto:sub (mats down)"
check "bash: subscription" "$(render_bash | classifier_segment)" "auto:sub (mats down)"

echo "=== both backends dead ==="
write_health dead
check "rust: dead" "$(render_rust | classifier_segment)" "auto"
check "bash: dead" "$(render_bash | classifier_segment)" "auto"

echo "=== a stale health file is treated as absent, not as news ==="
write_health subscription 25000   # ~7h, past the 6h cutoff
check "rust: stale renders nothing" "$(render_rust | classifier_segment)" ""
check "bash: stale renders nothing" "$(render_bash | classifier_segment)" ""

echo "=== no health file at all renders nothing ==="
rm -f "$HEALTH"
check "rust: absent renders nothing" "$(render_rust | classifier_segment)" ""
check "bash: absent renders nothing" "$(render_bash | classifier_segment)" ""

echo "=== a conf with no description degrades to a bare marker ==="
printf 'ANTHROPIC_API_KEY = ANTHROPIC_API_KEY\n' > "$FAKE/dot/config/secrets-global.conf"
write_health api
check "rust: bare marker" "$(render_rust | classifier_segment)" "auto"
check "bash: bare marker" "$(render_bash | classifier_segment)" "auto"

echo "=== PARITY: both implementations agree byte for byte ==="
printf '%s\n' \
    'ANTHROPIC_API_KEY = !ANTHROPIC_API_KEY - blockedone' \
    'ANTHROPIC_API_KEY = ANTHROPIC_API_KEY - mats' \
    > "$FAKE/dot/config/secrets-global.conf"
for backend in api subscription dead; do
    write_health "$backend"
    r=$(render_rust | classifier_segment)
    b=$(render_bash | classifier_segment)
    check "parity ($backend)" "$b" "$r"
done

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
