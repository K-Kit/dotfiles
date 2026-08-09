#!/usr/bin/env bash
# Global PreToolUse hook: BLOCKS permanent deletions via gws CLI.
# Deletions are irreversible across Google Workspace. Archiving/trashing is fine.
#
# Blocks across ALL gws services:
#   - gmail:    users messages delete, batchDelete, threads delete
#   - drive:    files delete, files emptyTrash, comments delete, drives delete,
#               permissions delete, replies delete, revisions delete, teamdrives delete
#   - calendar: acl delete, calendarList delete, calendars delete, calendars clear,
#               events delete
#   - tasks:    tasklists delete, tasks delete
#   - docs/sheets/slides/chat: any delete subcommand
#
# Also blocks the equivalent MCP connector calls, which carry no command field:
#   - mcp__claude_ai_Google_Calendar__delete_event
#   - mcp__claude_ai_Gmail__delete_label
#
# ALLOWS:
#   - trash/untrash (gmail, drive)
#   - modify/archive (gmail labels)
#   - --dry-run (in the same segment)
#   - --help (at end of the same segment)
#
# TOKENIZATION: the command is split into segments and each segment is
# normalised through shlex before matching. Matching the raw string fails OPEN
# two ways: `gws drive files de""lete` is executed by Bash as `delete` but never
# matches a naive regex, and a `--help` anywhere in the line would exempt an
# unrelated `... delete ; : --help` segment. Both are silent permission.
#
# Reads Bash tool_input JSON from stdin, checks the command field.
# Exit 0 = allow, Exit 2 = block.

set -uo pipefail

INPUT=$(cat)

# Extract tool_name, then one shlex-normalised segment per line. MCP tool calls
# carry NO command field, so a command-only hook would silently allow them —
# hence the tool_name branch below.
FIELDS=$(printf '%s' "$INPUT" | python3 -c "
import sys, json, shlex, re

SPLIT = re.compile(r'\|\||&&|[;\n|]')
SHELLS = {'bash', 'sh', 'zsh', 'dash', 'ksh', 'ash'}


def segments(cmd, depth=0):
    out = []
    for seg in SPLIT.split(cmd):
        seg = seg.strip()
        if not seg:
            continue
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = seg.split()
        if not toks:
            continue
        out.append(' '.join(toks))
        # \`bash -c '<nested>'\` keeps the whole nested command as one token.
        if depth < 4:
            for i, t in enumerate(toks):
                if t.rsplit('/', 1)[-1] not in SHELLS:
                    continue
                for j in range(i + 1, len(toks) - 1):
                    if toks[j] == '-c':
                        out.extend(segments(toks[j + 1], depth + 1))
                        break
    return out


try:
    d = json.load(sys.stdin)
    if not isinstance(d, dict):
        raise ValueError
    inp = d.get('tool_input', d)
    if not isinstance(inp, dict):
        inp = {}
    print(d.get('tool_name', '') or '')
    for s in segments(inp.get('command', '') or ''):
        print(s)
except Exception:
    print('PARSE_ERROR')
" 2>/dev/null)

if [ -z "$FIELDS" ]; then
    FIELDS="PARSE_ERROR"
fi

TOOL=$(printf '%s' "$FIELDS" | sed -n '1p')

# --- MCP connector deletions (no command field; matched by tool name) ---
# Google Calendar delete_event and Gmail delete_label are irreversible calls
# reachable without ever touching Bash.
case "$TOOL" in
    mcp__*Google_Calendar__delete_event)
        printf 'BLOCKED: Calendar event deletion via MCP not allowed.\n' >&2
        printf 'Deletions are irreversible. Cancel/decline the event, or delete via Calendar UI.\n' >&2
        exit 2
        ;;
    mcp__*Gmail__delete_label)
        printf 'BLOCKED: Gmail label deletion via MCP not allowed.\n' >&2
        printf 'Deleting a label is irreversible and unlabels every message using it.\n' >&2
        printf 'Use update_label to rename, or delete via the Gmail UI.\n' >&2
        exit 2
        ;;
esac

# Fail CLOSED: if the payload could not be parsed but the raw text looks like a
# gws deletion, refuse rather than guess.
if [ "$TOOL" = "PARSE_ERROR" ]; then
    if printf '%s' "$INPUT" | grep -q 'gws' && \
       printf '%s' "$INPUT" | grep -qE 'delete|batchDelete|emptyTrash|clear'; then
        printf 'BLOCKED: could not parse this command, and it mentions a gws deletion.\n' >&2
        printf 'Rewrite it as a plain, unquoted command so the guard can inspect it.\n' >&2
        exit 2
    fi
    exit 0
fi

check_segment() {
    local seg="$1"

    # Not a gws call — nothing to gate.
    printf '%s' "$seg" | grep -q 'gws' || return 0

    # Safe-flag exemptions apply ONLY within this segment.
    printf '%s' "$seg" | grep -qE '\-\-help[[:space:]]*$' && return 0
    printf '%s' "$seg" | grep -qE '(^|[[:space:]])--dry-run([[:space:]]|$)' && return 0

    # --- Gmail permanent deletions ---
    if printf '%s' "$seg" | grep -qE 'gws.*gmail.*users.*(messages|threads)[[:space:]]+delete'; then
        printf 'BLOCKED: Permanent Gmail deletion not allowed.\n' >&2
        printf 'Use "trash" instead of "delete" to move to trash.\n' >&2
        exit 2
    fi

    if printf '%s' "$seg" | grep -qE 'gws.*gmail.*users.*messages[[:space:]]+batchDelete'; then
        printf 'BLOCKED: Permanent Gmail batch deletion not allowed.\n' >&2
        printf 'Use "batchModify" to move messages to trash instead.\n' >&2
        exit 2
    fi

    # --- Drive permanent deletions ---
    if printf '%s' "$seg" | grep -qE 'gws.*drive.*files[[:space:]]+(delete|emptyTrash)'; then
        printf 'BLOCKED: Permanent Drive file deletion not allowed.\n' >&2
        printf 'Use Drive UI to trash files, or "files update" with trashed=true.\n' >&2
        exit 2
    fi

    if printf '%s' "$seg" | grep -qE 'gws.*drive.*(comments|drives|permissions|replies|revisions|teamdrives)[[:space:]]+delete'; then
        printf 'BLOCKED: Permanent Drive resource deletion not allowed.\n' >&2
        printf 'Deletions are irreversible. Manage via Drive UI instead.\n' >&2
        exit 2
    fi

    # --- Calendar deletions ---
    if printf '%s' "$seg" | grep -qE 'gws.*calendar.*(acl|calendarList|calendars|events)[[:space:]]+delete'; then
        printf 'BLOCKED: Calendar deletion not allowed.\n' >&2
        printf 'Manage calendar deletions via Calendar UI instead.\n' >&2
        exit 2
    fi

    if printf '%s' "$seg" | grep -qE 'gws.*calendar.*calendars[[:space:]]+clear'; then
        printf 'BLOCKED: Calendar clear (delete all events) not allowed.\n' >&2
        printf 'This would delete ALL events. Manage via Calendar UI instead.\n' >&2
        exit 2
    fi

    # --- Tasks deletions ---
    if printf '%s' "$seg" | grep -qE 'gws.*tasks.*(tasklists|tasks)[[:space:]]+delete'; then
        printf 'BLOCKED: Task deletion not allowed.\n' >&2
        printf 'Manage task deletions via Tasks UI instead.\n' >&2
        exit 2
    fi

    # --- Catch-all for any other gws service + delete ---
    # Covers docs, sheets, slides, chat, and future services
    if printf '%s' "$seg" | grep -qE 'gws[[:space:]]+\S+.*\bdelete\b'; then
        printf 'BLOCKED: Deletion via gws CLI not allowed.\n' >&2
        printf 'Deletions are irreversible. Manage via the Google Workspace UI.\n' >&2
        exit 2
    fi

    return 0
}

# Segments start at line 2 (line 1 is the tool name).
while IFS= read -r SEGMENT; do
    [ -n "$SEGMENT" ] && check_segment "$SEGMENT"
done < <(printf '%s\n' "$FIELDS" | tail -n +2)

exit 0
