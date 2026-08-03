#!/usr/bin/env bash
# Tests for secrets-use — the CLI that edits config/secrets-global.conf.
# Run: bash tests/test_secrets_use.sh
#
# Hermetic: runs dotfiles-secrets under DOTFILES_SECRETS_BACKEND=fixture and
# points secrets-use at a shim PATH, so no BWS token, no network and no real
# secret is involved.
#
# The assertion that matters most here is the byte-identical round trip:
# `--next` must only mark the active key blocked, never reorder or regenerate
# the block, so `--activate` reverses it exactly. A writer that regenerated the
# env name's lines would pass every "is the right key active" check and still
# quietly eat the user's comments and ordering.

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
USE_BIN="$REPO/custom_bins/secrets-use"
PASS=0
FAIL=0

TMP_ROOT="$REPO/tmp"
mkdir -p "$TMP_ROOT"
FIXTURE=$(mktemp -d "$TMP_ROOT/secrets-use.XXXXXX") || {
    echo "could not create fixture dir under $TMP_ROOT" >&2; exit 1; }
[[ -n "$FIXTURE" && -d "$FIXTURE" ]] || exit 1
trap 'rm -rf "$FIXTURE"' EXIT

CONF="$FIXTURE/scope.conf"

# --- fixture backend data ----------------------------------------------------
cat > "$FIXTURE/secrets" <<'EOF'
ANTHROPIC_API_KEY=fake-anthropic
HF_TOKEN=fake-hf
EOF

printf '%s\n' \
    "ANTHROPIC_API_KEY	ANTHROPIC_API_KEY - alpha	alpha note	uuid-alpha" \
    "ANTHROPIC_API_KEY	ANTHROPIC_API_KEY - beta	 	uuid-beta" \
    "ANTHROPIC_API_KEY	ANTHROPIC_API_KEY - gamma	 	uuid-gamma" \
    "HF_TOKEN	HF_TOKEN	 	uuid-hf" \
    > "$FIXTURE/meta"
: > "$FIXTURE/raw"

# --- shim PATH ---------------------------------------------------------------
# secrets-use calls `dotfiles-secrets` off PATH. The shim pins the fixture
# backend and the fixture conf so the real machine conf is never touched.
mkdir -p "$FIXTURE/bin"
cat > "$FIXTURE/bin/dotfiles-secrets" <<EOF
#!/usr/bin/env bash
exec env DOTFILES_SECRETS_BACKEND=fixture \\
         DOTFILES_SECRETS_FIXTURE_DIR="$FIXTURE" \\
         DOTFILES_SECRETS_GLOBAL_CONF="$CONF" \\
         "$REPO/custom_bins/dotfiles-secrets" "\$@"
EOF
chmod +x "$FIXTURE/bin/dotfiles-secrets"
export PATH="$FIXTURE/bin:$PATH"

use() { "$USE_BIN" "$@" 2>&1; }

check() {
    local desc="$1" got="$2" want="$3"
    if [[ "$got" == *"$want"* ]]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        printf 'FAIL: %s\n  wanted to contain: %s\n  got: %s\n' \
            "$desc" "$want" "$(printf '%s' "$got" | head -8 | tr '\n' '|')"
    fi
}

check_eq() {
    local desc="$1" got="$2" want="$3"
    if [[ "$got" == "$want" ]]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        printf 'FAIL: %s\n  wanted exactly: [%s]\n  got:            [%s]\n' \
            "$desc" "$want" "$got"
    fi
}

active() { dotfiles-secrets scope-entries "$1" | awk -F'\t' '$1=="active"{print $2; exit}'; }

# A conf with comments and an unrelated env name, so we can prove they survive.
seed_conf() {
    cat > "$CONF" <<'EOF'
# machine conf — comments must survive every edit
HF_TOKEN = HF_TOKEN

# anthropic below
ANTHROPIC_API_KEY = ANTHROPIC_API_KEY - alpha
ANTHROPIC_API_KEY = ANTHROPIC_API_KEY - beta

# trailing comment
EOF
}

echo "=== --list shows preference order and marks the active key ==="
seed_conf
out=$(use ANTHROPIC_API_KEY --list)
check "lists alpha"   "$out" "ANTHROPIC_API_KEY - alpha"
check "lists beta"    "$out" "ANTHROPIC_API_KEY - beta"
check "marks active"  "$out" "* ANTHROPIC_API_KEY - alpha"

echo "=== --next blocks the active key and promotes the next ==="
seed_conf
out=$(use ANTHROPIC_API_KEY --next)
check "reports old -> new" "$out" "ANTHROPIC_API_KEY - alpha -> ANTHROPIC_API_KEY - beta"
check_eq "beta is now active" "$(active ANTHROPIC_API_KEY)" "ANTHROPIC_API_KEY - beta"

echo "=== --next then --activate restores the file BYTE FOR BYTE ==="
seed_conf
before=$(cat "$CONF")
use ANTHROPIC_API_KEY --next >/dev/null
mid=$(cat "$CONF")
use ANTHROPIC_API_KEY --activate alpha >/dev/null
after=$(cat "$CONF")
if [[ "$before" == "$mid" ]]; then
    FAIL=$((FAIL + 1)); echo "FAIL: --next did not change the conf at all"
else
    PASS=$((PASS + 1))
fi
check_eq "round trip is byte-identical" "$after" "$before"

echo "=== edits preserve comments and unrelated env names ==="
seed_conf
use ANTHROPIC_API_KEY gamma >/dev/null
out=$(cat "$CONF")
check "keeps header comment"   "$out" "# machine conf — comments must survive every edit"
check "keeps inline comment"   "$out" "# anthropic below"
check "keeps trailing comment" "$out" "# trailing comment"
check "keeps other env name"   "$out" "HF_TOKEN = HF_TOKEN"

echo "=== setting a key promotes it and keeps the others as fallbacks ==="
seed_conf
use ANTHROPIC_API_KEY gamma >/dev/null
check_eq "gamma active" "$(active ANTHROPIC_API_KEY)" "ANTHROPIC_API_KEY - gamma"
entries=$(dotfiles-secrets scope-entries ANTHROPIC_API_KEY | awk -F'\t' '{print $2}' | tr '\n' ',')
check_eq "order: gamma, alpha, beta" "$entries" \
    "ANTHROPIC_API_KEY - gamma,ANTHROPIC_API_KEY - alpha,ANTHROPIC_API_KEY - beta,"

echo "=== setting the already-active key is a no-op ==="
seed_conf
before=$(cat "$CONF")
out=$(use ANTHROPIC_API_KEY alpha)
check "says already active" "$out" "already active"
check_eq "conf untouched" "$(cat "$CONF")" "$before"

echo "=== a substring resolves; an ambiguous or unknown one is refused ==="
seed_conf
out=$(use ANTHROPIC_API_KEY beta)
check "substring works" "$out" "ANTHROPIC_API_KEY - beta"

seed_conf
out=$(use ANTHROPIC_API_KEY ANTHROPIC; rc=$?; echo "rc=$rc")
check "ambiguous substring refused" "$out" "matches 3 keys"
check "ambiguous exits non-zero"    "$out" "rc=1"

out=$(use ANTHROPIC_API_KEY nosuchkey; rc=$?; echo "rc=$rc")
check "unknown key refused"   "$out" "no BWS key for 'ANTHROPIC_API_KEY' matches 'nosuchkey'"
check "unknown exits non-zero" "$out" "rc=1"

echo "=== an unknown env name is refused ==="
out=$(use NOT_A_REAL_ENV alpha; rc=$?; echo "rc=$rc")
check "refuses unknown env" "$out" "no BWS key uses env name 'NOT_A_REAL_ENV'"
check "exits non-zero"      "$out" "rc=1"

echo "=== blocking the last active key fails loud rather than leaving nothing ==="
cat > "$CONF" <<'EOF'
ANTHROPIC_API_KEY = ANTHROPIC_API_KEY - alpha
EOF
out=$(use ANTHROPIC_API_KEY --next; rc=$?; echo "rc=$rc")
check "warns no active key remains" "$out" "NO active key remains"
check "suggests the reversal"       "$out" "--activate"
check "exits non-zero"              "$out" "rc=1"

echo "=== declaring a name that had no line appends it ==="
cat > "$CONF" <<'EOF'
# only an unrelated name here
HF_TOKEN = HF_TOKEN
EOF
use ANTHROPIC_API_KEY alpha >/dev/null
check_eq "alpha active" "$(active ANTHROPIC_API_KEY)" "ANTHROPIC_API_KEY - alpha"
check "unrelated name kept" "$(cat "$CONF")" "HF_TOKEN = HF_TOKEN"

echo "=== the conf is written 0600 (it names real accounts) ==="
seed_conf
use ANTHROPIC_API_KEY gamma >/dev/null
mode=$(stat -c '%a' "$CONF" 2>/dev/null || stat -f '%Lp' "$CONF" 2>/dev/null)
check_eq "mode 600" "$mode" "600"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
