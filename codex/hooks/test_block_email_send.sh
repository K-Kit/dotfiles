#!/usr/bin/env bash
# Tests for block_email_send.sh hook
# Run: bash ~/.claude/hooks/test_block_email_send.sh

set -euo pipefail

# Resolve the hook NEXT TO this test, not through $HOME. ~/.claude points at the
# main checkout, so a worktree copy of the suite would silently exercise the
# deployed hook instead of the one it ships with — green here, unchanged code
# there. The sibling path is the only one that tests what the commit changes.
HOOK="$(cd "$(dirname "$0")" && pwd)/block_email_send.sh"
PASS=0
FAIL=0

test_case() {
    local description="$1"
    local input="$2"
    local expected_exit="$3"

    actual_exit=0
    printf '%s' "$input" | bash "$HOOK" >/dev/null 2>&1 || actual_exit=$?

    if [ "$actual_exit" -eq "$expected_exit" ]; then
        printf '  PASS: %s (exit %d)\n' "$description" "$actual_exit"
        PASS=$((PASS + 1))
    else
        printf '  FAIL: %s (expected exit %d, got %d)\n' "$description" "$expected_exit" "$actual_exit"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== SHOULD BLOCK (exit 2) ==="

test_case "gws gmail +send without --draft" \
    '{"tool_input":{"command":"gws gmail +send --to test@test.com --subject hi --body hello"}}' 2

test_case "gws gmail users drafts send" \
    '{"tool_input":{"command":"gws gmail users drafts send --params {\"userId\":\"me\"} --json {\"id\":\"abc\"}"}}' 2

test_case "gws gmail users messages send" \
    '{"tool_input":{"command":"gws gmail users messages send --params {\"userId\":\"me\"}"}}' 2

test_case "gws gmail +reply without --draft" \
    '{"tool_input":{"command":"gws gmail +reply --message-id abc --body hi"}}' 2

test_case "gws gmail +reply-all without --draft" \
    '{"tool_input":{"command":"gws gmail +reply-all --message-id abc --body hi"}}' 2

test_case "gws gmail +forward without --draft" \
    '{"tool_input":{"command":"gws gmail +forward --message-id abc --to test@test.com"}}' 2

test_case "gws gmail +send with extra spaces" \
    '{"tool_input":{"command":"gws   gmail   +send --to x@x.com --body hi"}}' 2

echo ""
echo "=== SHOULD ALLOW (exit 0) ==="

test_case "gws gmail +send --draft" \
    '{"tool_input":{"command":"gws gmail +send --to test@test.com --subject hi --body hello --draft"}}' 0

test_case "gws gmail +reply --draft" \
    '{"tool_input":{"command":"gws gmail +reply --message-id abc --body hi --draft"}}' 0

test_case "gws gmail +reply-all --draft" \
    '{"tool_input":{"command":"gws gmail +reply-all --message-id abc --body hi --draft"}}' 0

test_case "gws gmail +forward --draft" \
    '{"tool_input":{"command":"gws gmail +forward --message-id abc --to x@x.com --draft"}}' 0

test_case "gws gmail +send --help" \
    '{"tool_input":{"command":"gws gmail +send --help"}}' 0

test_case "gws gmail +reply --help" \
    '{"tool_input":{"command":"gws gmail +reply --help"}}' 0

test_case "gws gmail +send --dry-run" \
    '{"tool_input":{"command":"gws gmail +send --to x@x.com --body hi --dry-run"}}' 0

test_case "gws gmail +triage (read-only)" \
    '{"tool_input":{"command":"gws gmail +triage"}}' 0

test_case "gws gmail +read (read-only)" \
    '{"tool_input":{"command":"gws gmail +read --id abc"}}' 0

test_case "gws gmail users messages list" \
    '{"tool_input":{"command":"gws gmail users messages list --params {\"userId\":\"me\"}"}}' 0

test_case "gws gmail users drafts list" \
    '{"tool_input":{"command":"gws gmail users drafts list --params {\"userId\":\"me\"}"}}' 0

test_case "gws gmail users drafts create (create draft, not send)" \
    '{"tool_input":{"command":"gws gmail users drafts create --params {\"userId\":\"me\"}"}}' 0

test_case "git status (non-email)" \
    '{"tool_input":{"command":"git status"}}' 0

test_case "gws calendar events list (non-email)" \
    '{"tool_input":{"command":"gws calendar events list --params {}"}}' 0

test_case "empty command" \
    '{"tool_input":{"command":""}}' 0

test_case "no command field" \
    '{"tool_input":{"text":"hello"}}' 0

echo ""
echo "=== BYPASS PREVENTION (exit 2) ==="

test_case "gws gmail +send inside bash -c wrapper" \
    '{"tool_input":{"command":"bash -c \"gws gmail +send --to x@x.com --body hi\""}}' 2

test_case "gws gmail +send inside sh -c wrapper" \
    '{"tool_input":{"command":"sh -c \"gws gmail +send --to x@x.com --body hi\""}}' 2

test_case "gws gmail +send with --help in body text (not a real --help)" \
    '{"tool_input":{"command":"gws gmail +send --to x@x.com --body \"please see --help for info\""}}' 2

test_case "gws gmail +reply with --help in body text" \
    '{"tool_input":{"command":"gws gmail +reply --message-id abc --body \"check --help page\""}}' 2

test_case "gws gmail users messages send" \
    '{"tool_input":{"command":"gws gmail users messages send --json {}"}}' 2

echo ""
echo "=== HELP/DRY-RUN EDGE CASES (exit 0) ==="

test_case "gws gmail +send --help at end of command" \
    '{"tool_input":{"command":"gws gmail +send --help"}}' 0

test_case "gws gmail +reply --help at end" \
    '{"tool_input":{"command":"gws gmail +reply --help"}}' 0

echo ""
echo ""
echo "=== Regression: fail-open paths found by review 2026-07-27 (exit 2) ==="
# Both cases below were PERMITTED while this suite was green.

# A flag is a position in argv, not a substring of the command. `--draft` sitting
# INSIDE a --body value is message text the user typed; reading it as an exemption
# lets a live send through. After shlex.split the whole body is ONE token, which
# is not equal to `--draft`.
test_case "--draft inside a --body value is text, not a flag" \
    '{"tool_input":{"command":"gws gmail +send --to a@b.com --body \"remind me to --draft this\""}}' 2

test_case "--draft inside a --subject value is text, not a flag" \
    '{"tool_input":{"command":"gws gmail +send --to a@b.com --subject \"re: --draft\" --body hi"}}' 2

# Bash executes `+se""nd` as `+send`; a regex over the raw string never matches.
test_case "empty-string split inside the verb" \
    '{"tool_input":{"command":"gws gmail +se\"\"nd --to a@b.com --body hi"}}' 2

test_case "single-quote split inside the verb" \
    "{\"tool_input\":{\"command\":\"gws gmail +se''nd --to a@b.com --body hi\"}}" 2

# --draft in its real argv position must still exempt — otherwise the fix above
# would have been "block everything", which is not a working gate.
test_case "real --draft flag still allowed alongside a body mentioning it" \
    '{"tool_input":{"command":"gws gmail +send --draft --to a@b.com --body \"about --draft\""}}' 0

echo ""
echo "=== RESULTS ==="
echo "PASS: $PASS  FAIL: $FAIL"
[ "$FAIL" -eq 0 ] && echo "All tests passed!" || echo "SOME TESTS FAILED"
exit "$FAIL"
