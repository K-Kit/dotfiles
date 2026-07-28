#!/usr/bin/env bash
# Tests for the convention nudges: nudge_lint.sh (ruff/shellcheck at write
# time) and nudge_md_hardwrap.py (one paragraph = one line).
#
# Same contract as test_substitution_guards.sh: each hook must (a) fire a
# systemMessage on a positive case, (b) stay silent on negatives, and
# (c) NEVER exit non-zero. Lint cases are skipped (not failed) when the
# linter isn't installed — the hook itself degrades the same way.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PASS=0
FAIL=0
SKIP=0

TMP=""
for cand in "${TMPDIR:-}" /tmp/claude /tmp .; do
    [ -n "$cand" ] || continue
    if mkdir -p "$cand/conv-nudge-tests.$$" 2>/dev/null; then
        TMP="$cand/conv-nudge-tests.$$"
        break
    fi
done
[ -n "$TMP" ] || { echo "no writable temp dir found" >&2; exit 1; }
trap 'rm -rf "$TMP"' EXIT

run() {
    local desc="$1" hook="$2" input="$3" expect="$4"
    local out rc=0

    case "$hook" in
        *.py) out=$(printf '%s' "$input" | python3 "$DIR/$hook" 2>/dev/null) || rc=$? ;;
        *)    out=$(printf '%s' "$input" | bash "$DIR/$hook" 2>/dev/null) || rc=$? ;;
    esac

    if [ "$rc" -ne 0 ]; then
        FAIL=$((FAIL + 1))
        printf 'FAIL: %s (nudge hook exited %d — must always be 0)\n' "$desc" "$rc"
        return
    fi

    local fired=silent
    case "$out" in *systemMessage*) fired=fire ;; esac

    if [ "$fired" = "$expect" ]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        printf 'FAIL: %s (expected %s, got %s)\n' "$desc" "$expect" "$fired"
    fi
}

# tool_json <tool> <path> <key> <value> — values via argv, never interpolated.
tool_json() {
    python3 -c "
import json, sys
tool, path, key, value = sys.argv[1:5]
print(json.dumps({'tool_name': tool, 'tool_input': {'file_path': path, key: value}}))
" "$1" "$2" "$3" "$4"
}

post_write() { tool_json Write "$1" content "$2"; }
post_edit()  { tool_json Edit "$1" new_string "$2"; }

# post_edits2 <path> <frag_a> <frag_b> — Edit with an edits[] array of two
# independent replacements (they must NOT be scanned as adjacent lines).
post_edits2() {
    python3 -c "
import json, sys
path, a, b = sys.argv[1:4]
print(json.dumps({'tool_name': 'Edit', 'tool_input': {'file_path': path,
    'edits': [{'old_string': 'x', 'new_string': a},
              {'old_string': 'y', 'new_string': b}]}}))
" "$1" "$2" "$3"
}

# --- nudge_lint.sh -----------------------------------------------------------
# The hook lints the file ON DISK, so positive/negative fixtures are real files.
echo "=== lint nudge ==="

if command -v ruff >/dev/null 2>&1; then
    printf 'import os\nx = 1\n' > "$TMP/dirty.py"       # F401 unused import
    printf 'GOOD = 1\n'         > "$TMP/clean.py"
    run "dirty .py fires"  nudge_lint.sh "$(post_write "$TMP/dirty.py" "import os")" fire
    run "clean .py silent" nudge_lint.sh "$(post_write "$TMP/clean.py" "GOOD = 1")"  silent
else
    SKIP=$((SKIP + 2)); echo "SKIP: ruff not installed (2 cases)"
fi

if command -v shellcheck >/dev/null 2>&1; then
    # shellcheck disable=SC2016  # literal $1 is the fixture's lint violation
    printf 'echo $1\n'               > "$TMP/dirty.sh"  # SC2148 no shebang + SC2086
    printf '#!/bin/bash\necho "ok"\n' > "$TMP/clean.sh"
    run "dirty .sh fires"  nudge_lint.sh "$(post_write "$TMP/dirty.sh" "echo")" fire
    run "clean .sh silent" nudge_lint.sh "$(post_write "$TMP/clean.sh" "echo")" silent
else
    SKIP=$((SKIP + 2)); echo "SKIP: shellcheck not installed (2 cases)"
fi

run "missing file silent"   nudge_lint.sh "$(post_write "$TMP/never_written.py" "x")" silent
run "non-lintable .md"      nudge_lint.sh "$(post_write "$TMP/notes.md" "hi")"        silent
run "no file_path"          nudge_lint.sh '{"tool_name":"Write","tool_input":{}}'     silent
run "vendored path skipped" nudge_lint.sh "$(post_write /repo/node_modules/a.py "import os")" silent

# Round-6 adversarial-review regression: a hanging linter must not breach the
# fail-open contract — the hook's own watchdog (not GNU timeout, absent on
# stock macOS) must return exit 0 well before the external hook deadline.
mkdir -p "$TMP/bin"
printf '#!/bin/sh\nsleep 30\n' > "$TMP/bin/ruff"
chmod +x "$TMP/bin/ruff"
printf 'x = 1\n' > "$TMP/hang.py"
START=$SECONDS
rc=0
out=$(post_write "$TMP/hang.py" "x = 1" \
      | PATH="$TMP/bin:$PATH" bash "$DIR/nudge_lint.sh" 2>/dev/null) || rc=$?
ELAPSED=$((SECONDS - START))
if [ "$rc" -eq 0 ] && [ "$ELAPSED" -lt 8 ] && [ -z "$out" ]; then
    PASS=$((PASS + 1))
else
    FAIL=$((FAIL + 1))
    printf 'FAIL: hanging linter stays fail-open (rc=%d, elapsed=%ss)\n' "$rc" "$ELAPSED"
fi

# --- nudge_md_hardwrap.py ----------------------------------------------------
echo "=== markdown hard-wrap nudge ==="

WRAPPED='This sentence keeps going for a while and then breaks in the middle,
continuing on the next line against the one-paragraph-one-line rule.'
run "hard-wrapped prose" nudge_md_hardwrap.py \
    "$(post_write /repo/notes.md "$WRAPPED")" fire
run "Edit new_string wrapped" nudge_md_hardwrap.py \
    "$(post_edit /repo/notes.md "$WRAPPED")" fire

ONELINE='A complete paragraph living on a single line, exactly as the rule wants it to be.

Another complete paragraph, also on its own single line with a blank line between.'
run "one-line paragraphs" nudge_md_hardwrap.py \
    "$(post_write /repo/notes.md "$ONELINE")" silent

FENCED='```
this looks like wrapped prose but it sits inside a code fence and,
being code, is allowed to wrap wherever it likes without penalty
```'
run "wrap inside code fence" nudge_md_hardwrap.py \
    "$(post_write /repo/notes.md "$FENCED")" silent

LIST='- a bullet item that is quite long but bullets are non-prose and,
- another bullet, so neither line is treated as a wrapped paragraph'
run "bullet list" nudge_md_hardwrap.py \
    "$(post_write /repo/notes.md "$LIST")" silent

TABLE='| a long table cell that could look like prose to a naive check, |
| another row starting with a pipe so it is clearly a table |'
run "table rows" nudge_md_hardwrap.py \
    "$(post_write /repo/notes.md "$TABLE")" silent

# Round-6 adversarial-review regressions ------------------------------------

# Edit whose fragment sits inside a pre-existing fence: with the file on disk
# the hook must see the fence context and stay silent.
printf '%s\n' '# Doc' '' '```' \
    'first fenced line that is quite long and ends with a trailing comma,' \
    'continuing lowercase exactly as shell output legitimately does' \
    '```' > "$TMP/fenced_ctx.md"
INNER='first fenced line that is quite long and ends with a trailing comma,
continuing lowercase exactly as shell output legitimately does'
run "edit inside existing fence" nudge_md_hardwrap.py \
    "$(post_edit "$TMP/fenced_ctx.md" "$INNER")" silent

# Same disk-context path, but the edit genuinely IS wrapped prose → fire.
printf '%s\n' "$WRAPPED" > "$TMP/wrapped_ctx.md"
run "edit wrapped prose on disk" nudge_md_hardwrap.py \
    "$(post_edit "$TMP/wrapped_ctx.md" "$WRAPPED")" fire

# Two separate edits[] fragments must not be scanned as adjacent lines
# (unreadable path → per-fragment fallback).
run "separate edits not adjacent" nudge_md_hardwrap.py \
    "$(post_edits2 /repo/notes.md \
        "This first edit is long enough to look like prose and ends with a comma," \
        "another edit that starts lowercase but belongs somewhere else entirely")" silent

# GFM table rows written without outer pipes are still a table, not prose.
NOPIPE='Ada Lovelace | wrote the first published algorithm and was a visionary,
her colleague | designed the analytical engine she wrote about at length'
run "pipe-less table rows" nudge_md_hardwrap.py \
    "$(post_write /repo/notes.md "$NOPIPE")" silent

# ``` inside a ````-fence must not toggle fence state.
QUADFENCE='````markdown
```
inner text long enough to resemble wrapped prose ending with a comma,
continuing lowercase inside the nested example fence
```
````'
run "nested 4-backtick fence" nudge_md_hardwrap.py \
    "$(post_write /repo/notes.md "$QUADFENCE")" silent

run "non-md path" nudge_md_hardwrap.py \
    "$(post_write /repo/notes.txt "$WRAPPED")" silent
run "archived md skipped" nudge_md_hardwrap.py \
    "$(post_write /repo/archive/old.md "$WRAPPED")" silent
run "non-dict JSON" nudge_md_hardwrap.py '[]' silent

echo
TOTAL=$((PASS + FAIL))
echo "Results: $PASS passed, $FAIL failed (total $TOTAL, skipped $SKIP)"
[ "$FAIL" -eq 0 ] && echo "All tests passed!"
[ "$FAIL" -eq 0 ]
