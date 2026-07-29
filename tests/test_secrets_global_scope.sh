#!/usr/bin/env bash
# Tests for global-scope BWS key disambiguation in dotfiles-secrets.
# Run: bash tests/test_secrets_global_scope.sh
#
# Fully hermetic: fixture caches in a temp CACHE_DIR, fresh mtimes so nothing
# refreshes against live BWS, and fake values throughout. No real secret is
# ever read or printed by this suite.

set -uo pipefail

BIN="$(cd "$(dirname "$0")/.." && pwd)/custom_bins/dotfiles-secrets"
PASS=0
FAIL=0

# Repo-local tmp/ (gitignored): $TMPDIR is not reliably writable under the
# Claude Code sandbox, and this keeps the fixture portable either way.
TMP_ROOT="$(cd "$(dirname "$0")/.." && pwd)/tmp"
mkdir -p "$TMP_ROOT"
# Abort if the fixture cannot be created. Without `set -e` an empty FIXTURE would
# make DOTFILES_SECRETS_CACHE_DIR="" below, which the helper reads as "unset" and
# falls back to the LIVE cache — the suite would then exercise real secrets and
# could surface one in a failure diagnostic.
FIXTURE=$(mktemp -d "$TMP_ROOT/secrets-scope.XXXXXX") || {
    echo "could not create fixture dir under $TMP_ROOT" >&2; exit 1; }
[[ -n "$FIXTURE" && -d "$FIXTURE" ]] || {
    echo "fixture dir is not usable: ${FIXTURE:-<empty>}" >&2; exit 1; }
trap 'rm -rf "$FIXTURE"' EXIT

# The helper checks for a BWS token before it ever reads these caches, so without
# a fake one the whole suite would exit early on any machine lacking live BWS
# credentials (CI, a fresh clone) — passing here only because this box has one.
export BWS_ACCESS_TOKEN="fixture-token-not-real"

# --- fixture caches --------------------------------------------------------
# Two ANTHROPIC keys (ambiguous), one HF_TOKEN (unambiguous), and one key whose
# label contains both " - " and ":" to prove the parser handles real labels.
b64() { printf '%s' "$1" | base64 | tr -d '\n'; }

cat > "$FIXTURE/secrets.bws.cache" <<EOF
ANTHROPIC_API_KEY=fake-anthropic-alpha
HF_TOKEN=fake-hf
RUNPOD_API_KEY=fake-runpod-one
EOF

printf '%s\n' \
    "ANTHROPIC_API_KEY	ANTHROPIC_API_KEY - alpha	" \
    "ANTHROPIC_API_KEY	ANTHROPIC_API_KEY - beta gamma	" \
    "HF_TOKEN	HF_TOKEN	" \
    "RUNPOD_API_KEY	RUNPOD_API_KEY - one:Two	" \
    "RUNPOD_API_KEY	RUNPOD_API_KEY - three	" \
    > "$FIXTURE/meta.bws.cache"

printf '%s\n' \
    "ANTHROPIC_API_KEY - alpha	$(b64 fake-anthropic-alpha)		uuid-alpha" \
    "ANTHROPIC_API_KEY - beta gamma	$(b64 fake-anthropic-beta)		uuid-beta" \
    "HF_TOKEN	$(b64 fake-hf)		uuid-hf" \
    "RUNPOD_API_KEY - one:Two	$(b64 fake-runpod-one)		uuid-rp1" \
    "RUNPOD_API_KEY - three	$(b64 fake-runpod-three)		uuid-rp3" \
    > "$FIXTURE/raw.bws.cache"

# Run the binary against the fixtures with a given secrets-global.conf body.
# Prints "<exit status>\n<stdout>\n---STDERR---\n<stderr>".
run_with_conf() {
    local conf_body="$1"; shift
    printf '%s\n' "$conf_body" > "$FIXTURE/scope.conf"
    local out err rc=0
    err="$FIXTURE/stderr.txt"
    out=$(DOTFILES_SECRETS_BACKEND=bws \
          DOTFILES_SECRETS_CACHE_DIR="$FIXTURE" \
          DOTFILES_SECRETS_GLOBAL_CONF="$FIXTURE/scope.conf" \
          "$BIN" "$@" 2>"$err") || rc=$?
    printf '%s\n%s\n---STDERR---\n%s\n' "$rc" "$out" "$(cat "$err")"
}

check() {
    local desc="$1" got="$2" want="$3"
    if [[ "$got" == *"$want"* ]]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        printf 'FAIL: %s\n  wanted to contain: %s\n  got: %s\n' \
            "$desc" "$want" "$(printf '%s' "$got" | head -6 | tr '\n' '|')"
    fi
}

echo "=== ambiguous name, default declared -> resolves to the declared key ==="
R=$(run_with_conf 'ANTHROPIC_API_KEY = ANTHROPIC_API_KEY - beta gamma' shell ANTHROPIC_API_KEY)
check "exit 0"                "$R" $'0\n'
check "exports the beta value" "$R" "fake-anthropic-beta"

echo "=== ambiguous name, nothing declared -> dies, names the candidates ==="
R=$(run_with_conf '# nothing declared' shell ANTHROPIC_API_KEY)
check "non-zero exit"         "$R" $'1\n'
check "no value on stdout"    "$R" $'1\n\n---STDERR---'
check "says ambiguous"        "$R" "Ambiguous env name 'ANTHROPIC_API_KEY'"
check "lists candidate alpha" "$R" "ANTHROPIC_API_KEY - alpha"
check "lists candidate beta"  "$R" "ANTHROPIC_API_KEY - beta gamma"
check "points at the conf"    "$R" "scope.conf"

echo "=== declared key that does not exist -> dies, does not fall through ==="
R=$(run_with_conf 'ANTHROPIC_API_KEY = ANTHROPIC_API_KEY - typo' shell ANTHROPIC_API_KEY)
check "non-zero exit"      "$R" $'1\n'
check "names the bad map"  "$R" "which is not one of its BWS keys"

echo "=== unambiguous name -> unaffected by the map ==="
R=$(run_with_conf '# empty' shell HF_TOKEN)
check "exit 0"          "$R" $'0\n'
check "exports HF"      "$R" "fake-hf"

echo "=== a named key that is absent -> dies (used to warn and exit 0) ==="
R=$(run_with_conf '# empty' shell NOT_A_REAL_KEY)
check "non-zero exit"   "$R" $'1\n'
check "says not found"  "$R" "not found in encrypted secrets"

echo "=== --all is best-effort: skips undeclared-ambiguous, keeps the rest ==="
R=$(run_with_conf '# nothing declared' shell --all)
check "exit 0"                  "$R" $'0\n'
check "still exports HF_TOKEN"  "$R" "fake-hf"
check "warns about the skip"    "$R" "skipping ambiguous env name 'ANTHROPIC_API_KEY'"
if [[ "$R" == *"export ANTHROPIC_API_KEY"* ]]; then
    FAIL=$((FAIL + 1)); echo "FAIL: --all exported an ambiguous undeclared key"
else
    PASS=$((PASS + 1))
fi

echo "=== --all with a declaration exports the declared one ==="
R=$(run_with_conf 'ANTHROPIC_API_KEY = ANTHROPIC_API_KEY - alpha' shell --all)
check "exit 0"            "$R" $'0\n'
check "exports alpha"     "$R" "fake-anthropic-alpha"

echo "=== get-value honours the map for a bare ambiguous name ==="
R=$(run_with_conf 'ANTHROPIC_API_KEY = ANTHROPIC_API_KEY - beta gamma' get-value ANTHROPIC_API_KEY)
check "exit 0"        "$R" $'0\n'
check "returns beta"  "$R" "fake-anthropic-beta"

echo "=== get-value with an exact key still bypasses the map entirely ==="
R=$(run_with_conf 'ANTHROPIC_API_KEY = ANTHROPIC_API_KEY - beta gamma' get-value 'ANTHROPIC_API_KEY - alpha')
check "exit 0"         "$R" $'0\n'
check "returns alpha"  "$R" "fake-anthropic-alpha"

echo "=== get-value on an ambiguous undeclared name still dies ==="
R=$(run_with_conf '# empty' get-value ANTHROPIC_API_KEY)
check "non-zero exit" "$R" $'1\n'
check "says ambiguous" "$R" "Ambiguous env name"

echo "=== conf parsing: labels with ':' and ' - ', comments, odd whitespace ==="
R=$(run_with_conf "$(printf '%s\n' \
        '# a comment' \
        '' \
        '   RUNPOD_API_KEY   =   RUNPOD_API_KEY - one:Two   ' \
        'HF_TOKEN = HF_TOKEN')" shell RUNPOD_API_KEY)
check "exit 0"                "$R" $'0\n'
check "colon label resolves"  "$R" "fake-runpod-one"

R=$(run_with_conf 'ANTHROPIC_API_KEY=ANTHROPIC_API_KEY - alpha' shell ANTHROPIC_API_KEY)
check "no spaces around ="    "$R" "fake-anthropic-alpha"

echo "=== a commented-out declaration counts as undeclared ==="
R=$(run_with_conf '# ANTHROPIC_API_KEY = ANTHROPIC_API_KEY - alpha' shell ANTHROPIC_API_KEY)
check "non-zero exit"  "$R" $'1\n'
check "says ambiguous" "$R" "Ambiguous env name"

echo "=== a missing conf file is not an error for unambiguous names ==="
rm -f "$FIXTURE/scope.conf"
out=$(DOTFILES_SECRETS_BACKEND=bws DOTFILES_SECRETS_CACHE_DIR="$FIXTURE" \
      DOTFILES_SECRETS_GLOBAL_CONF="$FIXTURE/nonexistent.conf" \
      "$BIN" shell HF_TOKEN 2>/dev/null)
check "exports HF without a conf" "$out" "fake-hf"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
